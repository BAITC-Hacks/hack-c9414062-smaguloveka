use axum::{Router,Json,extract::{State,Path,Multipart,DefaultBodyLimit,Request},routing::{get,post,put},http::{StatusCode,HeaderMap},response::{IntoResponse,Response},middleware::{self,Next}};
use serde_json::{Value,json};
use crate::{store::{AppState,err},domain::*,ai,models,export,integrations,Result,ApiError};
use tokio::io::AsyncWriteExt;
use tower_http::services::{ServeDir,ServeFile};

pub fn router(s:AppState,web:std::path::PathBuf)->Router{
    let api=Router::new()
        .route("/api/meetings",get(meetings).post(create_meeting))
        .route("/api/meetings/{id}",get(meeting).put(update_meeting))
        .route("/api/meetings/{id}/process",post(process))
        .route("/api/meetings/{id}/audio",get(audio))
        .route("/api/meetings/{id}/export/{format}",get(download_export))
        .route("/api/settings",get(settings).put(save_settings))
        .route("/api/models",get(list_models))
        .route("/api/models/{id}/download",post(download_model))
        .route("/api/ollama/models",get(ollama_models))
        .route("/api/ollama/pull",post(ollama_pull))
        .route("/api/integrations",get(integrations::list).post(integrations::save))
        .route("/api/integrations/{id}",put(integrations::update).delete(integrations::remove))
        .route("/api/deliveries",get(integrations::deliveries))
        .route("/api/meetings/{id}/send/{destination}",post(integrations::send))
        .with_state(s);
    crate::app().merge(api).fallback_service(ServeDir::new(web).append_index_html_on_directories(true))
        .layer(DefaultBodyLimit::max(512*1024*1024))
        .layer(middleware::from_fn(local_boundary))
}
async fn local_boundary(req:Request,next:Next)->Response{
    let host=req.headers().get("host").and_then(|v|v.to_str().ok()).unwrap_or("");
    if !["localhost:8080","127.0.0.1:8080","[::1]:8080"].contains(&host){return (StatusCode::FORBIDDEN,"Приложение доступно только через localhost:8080").into_response()}
    if let Some(origin)=req.headers().get("origin") {if origin.to_str().ok()!=Some(&format!("http://{host}")) {return (StatusCode::FORBIDDEN,"Cross-origin requests are not allowed").into_response()}}
    if !["GET","HEAD","OPTIONS"].contains(&req.method().as_str()) && req.headers().get("x-hatshy-client").and_then(|v|v.to_str().ok())!=Some("web") {return (StatusCode::FORBIDDEN,"Missing client header").into_response()}
    let mut response=next.run(req).await;let headers=response.headers_mut();
    headers.insert("x-content-type-options","nosniff".parse().unwrap());headers.insert("cross-origin-resource-policy","same-origin".parse().unwrap());headers.insert("x-frame-options","DENY".parse().unwrap());headers.insert("referrer-policy","no-referrer".parse().unwrap());
    headers.insert("content-security-policy","default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'".parse().unwrap());response
}
async fn meetings(State(s):State<AppState>)->Result<Json<Vec<Meeting>>>{Ok(Json(s.meetings().await?))}
async fn meeting(State(s):State<AppState>,Path(id):Path<String>)->Result<Json<Meeting>>{Ok(Json(s.meeting(&id).await?))}
async fn create_meeting(State(s):State<AppState>,mut form:Multipart)->Result<Json<Meeting>>{
    let id=uuid::Uuid::new_v4().to_string();let source=s.dir.join("audio").join(format!("{id}.source"));
    let result:std::result::Result<Meeting,String>=async{
        let mut title=String::new();let mut language="auto".to_string();let mut date=None;let mut audio=false;
        while let Some(mut field)=form.next_field().await.map_err(err)?{match field.name().unwrap_or(""){
            "file"=>{if audio{return Err("Допустим один файл".into())}let mut f=tokio::fs::File::create(&source).await.map_err(err)?;let mut size=0;while let Some(chunk)=field.chunk().await.map_err(err)?{size+=chunk.len();if size>500*1024*1024{return Err("Лимит файла 500 МБ".into())}f.write_all(&chunk).await.map_err(err)?;}if size==0{return Err("Пустой файл".into())}f.sync_all().await.map_err(err)?;audio=true;},
            "title"=>title=field.text().await.map_err(err)?,"language"=>language=field.text().await.map_err(err)?,"date"=>{let d=field.text().await.map_err(err)?;if !d.is_empty(){chrono::NaiveDate::parse_from_str(&d,"%Y-%m-%d").map_err(err)?;date=Some(d);}},_=>{}
        }}
        if title.trim().is_empty()||title.len()>500{return Err("Введите название до 500 символов".into())}if !["auto","ru","kk"].contains(&language.as_str()){return Err("Неверный язык".into())}if !audio{return Err("Выберите запись".into())}
        let m=Meeting{id,title:title.trim().into(),date,created_at:chrono::Utc::now().to_rfc3339(),language,status:"uploaded".into(),error:None,duration:0.0,segments:vec![],protocol:None,revision:0,audio,approved:false};s.insert_meeting(&m).await?;Ok(m)
    }.await;
    match result{Ok(m)=>Ok(Json(m)),Err(e)=>{let _=tokio::fs::remove_file(source).await;Err(e.into())}}
}
async fn update_meeting(State(s):State<AppState>,Path(id):Path<String>,Json(v):Json<Value>)->Result<Json<Meeting>>{
    let mut m=s.meeting(&id).await?;if ["queued","transcribing","summarizing"].contains(&m.status.as_str()){return Err("Дождитесь завершения обработки".into())}
    if v["revision"].as_i64()!=Some(m.revision){return Err("Конфликт версии: обновите страницу".into())}
    if let Some(title)=v["title"].as_str(){if title.trim().is_empty()||title.len()>500{return Err("Неверное название".into())}m.title=title.trim().into();}
    if let Some(segments)=v.get("segments"){let edited:Vec<Segment>=serde_json::from_value(segments.clone()).map_err(err)?;if edited.len()!=m.segments.len()||edited.iter().zip(&m.segments).any(|(a,b)|a.id!=b.id||a.start!=b.start||a.end!=b.end||a.text.len()>10000||a.speaker.as_ref().is_some_and(|s|s.len()>200)){return Err("Можно менять текст и говорящего, сохраняя таймкоды".into())}m.segments=edited;m.approved=false;}
    if let Some(protocol)=v.get("protocol"){let p:Protocol=serde_json::from_value(protocol.clone()).map_err(err)?;if p.actions.iter().any(|a|!["todo","doing","done"].contains(&a.status.as_str())||a.evidence.iter().any(|id|!m.segments.iter().any(|s|s.id==*id))){return Err("Неверные поручения".into())}m.protocol=Some(p);m.approved=false;}
    if let Some(approved)=v["approved"].as_bool(){if approved&&m.protocol.is_none(){return Err("Сначала сформируйте протокол".into())}m.approved=approved;}
    s.save_meeting(&mut m).await?;Ok(Json(m))
}
async fn process(State(s):State<AppState>,Path(id):Path<String>,Json(v):Json<Value>)->Result<Json<Meeting>>{
    let mut m=s.meeting(&id).await?;if ["queued","transcribing","summarizing"].contains(&m.status.as_str()){return Err("Запись уже в обработке".into())}
    let settings=s.settings().await?;let only_summary=v["summary_only"].as_bool().unwrap_or(false);
    if only_summary&&m.segments.is_empty(){return Err("Сначала получите транскрипт".into())}
    m.status="queued".into();m.error=None;m.approved=false;s.save_meeting(&mut m).await?;let response=m.clone();
    tokio::spawn(async move {
        let _permit=s.jobs.acquire().await.unwrap();
        let result:std::result::Result<(),String>=async{
            if !only_summary{m.status="transcribing".into();s.save_meeting(&mut m).await?;let segments=ai::transcribe(&s,&m,&settings).await?;m.duration=segments.last().map(|s|s.end).unwrap_or(0.0);m.segments=segments;m.protocol=None;s.save_meeting(&mut m).await?;}
            m.status="summarizing".into();s.save_meeting(&mut m).await?;m.protocol=Some(ai::summarize(&s,&m,&settings).await?);m.status="ready".into();s.save_meeting(&mut m).await?;Ok(())
        }.await;
        if let Err(e)=result{m.status="error".into();m.error=Some(e);let _=s.save_meeting(&mut m).await;}
    });Ok(Json(response))
}
async fn audio(State(s):State<AppState>,Path(id):Path<String>,req:Request)->Result<Response>{let m=s.meeting(&id).await?;if !m.audio{return Err("Аудио отсутствует".into())}let wav=s.dir.join("audio").join(format!("{id}.wav"));let path=if wav.exists(){wav}else{s.dir.join("audio").join(format!("{id}.source"))};use tower::ServiceExt;let r=ServeFile::new(path).oneshot(req).await.map_err(err)?;Ok(r.into_response())}
async fn download_export(State(s):State<AppState>,Path((id,format)):Path<(String,String)>)->Result<Response>{let m=s.meeting(&id).await?;let content=markdown(&m);let title=m.title.clone();let (bytes,mime)=match format.as_str(){"md"=>(content.into_bytes(),"text/markdown; charset=utf-8"),"json"=>(serde_json::to_vec_pretty(&m).map_err(err)?,"application/json"),"pdf"=>(tokio::task::spawn_blocking(move||export::render_pdf(&title,&content)).await.map_err(err)??,"application/pdf"),"docx"=>(export::render_docx(&title,&content)?,"application/vnd.openxmlformats-officedocument.wordprocessingml.document"),_=>return Err("Неизвестный формат".into())};let mut headers=HeaderMap::new();headers.insert("content-type",mime.parse().unwrap());headers.insert("content-disposition",format!("attachment; filename=\"protocol-{id}.{format}\"").parse().unwrap());Ok((headers,bytes).into_response())}
async fn settings(State(s):State<AppState>)->Result<Json<Value>>{let mut config=s.settings().await?;let has_key=!config.openai_key.is_empty();config.openai_key.clear();let mut v=serde_json::to_value(config).map_err(err)?;v["has_openai_key"]=json!(has_key);v["downloads"]=json!(*s.downloads.lock().await);Ok(Json(v))}
async fn save_settings(State(s):State<AppState>,Json(v):Json<Value>)->Result<Json<Value>>{let _lock=s.settings_lock.lock().await;let old=s.settings().await?;let mut config:Settings=serde_json::from_value(v.clone()).map_err(err)?;if config.openai_key.is_empty()&&!v["clear_openai_key"].as_bool().unwrap_or(false){config.openai_key=old.openai_key;}if !["builtin","ollama","openai"].contains(&config.provider.as_str()){return Err("Неизвестный провайдер".into())}if config.strict_local&&config.provider=="openai"{return Err("Отключите строгий режим для OpenAI — это выходит за ограничения ТЗ".into())}ai::local_url(&config.ollama_url)?;if models::find(&config.asr_model)?.kind!="asr"||models::find(&config.llm_model)?.kind!="llm"{return Err("Неверный тип модели".into())}s.save_settings(config).await?;Ok(Json(json!({"ok":true})))}
async fn list_models(State(s):State<AppState>)->Json<Vec<Value>>{Json(models::list(&s).await)}
async fn download_model(State(s):State<AppState>,Path(id):Path<String>)->Result<Json<Value>>{models::start(s,id).await?;Ok(Json(json!({"ok":true})))}
async fn ollama_models(State(s):State<AppState>)->Result<Json<Value>>{let settings=s.settings().await?;let url=ai::local_url(&settings.ollama_url)?;let r=ai::client()?.get(format!("{}/api/tags",url.as_str().trim_end_matches('/'))).send().await.map_err(|_|ApiError("Ollama недоступна".into()))?;if !r.status().is_success(){return Err("Ollama вернула ошибку".into())}Ok(Json(r.json().await.map_err(err)?))}
async fn ollama_pull(State(s):State<AppState>,Json(v):Json<Value>)->Result<Json<Value>>{let model=v["model"].as_str().ok_or("Укажите модель")?.to_string();if model.len()>150||model.contains("cloud"){return Err("Неверная локальная модель".into())}let settings=s.settings().await?;let url=ai::local_url(&settings.ollama_url)?;let key=format!("ollama:{model}");{let mut d=s.downloads.lock().await;if d.get(&key).is_some_and(|v|v["status"]=="downloading"){return Err("Модель уже скачивается".into())}d.insert(key.clone(),json!({"status":"downloading"}));}tokio::spawn(async move{let result=async{let r=ai::client()?.post(format!("{}/api/pull",url.as_str().trim_end_matches('/'))).json(&json!({"model":model,"stream":false})).send().await.map_err(|_|"Ollama недоступна")?;if !r.status().is_success(){return Err("Ollama не смогла скачать модель".to_string())}Ok(())}.await;s.downloads.lock().await.insert(key,match result{Ok(())=>json!({"status":"installed"}),Err(e)=>json!({"status":"error","error":e})});});Ok(Json(json!({"ok":true})))}
