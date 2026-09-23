use axum::{Json,extract::{State,Path}};
use serde_json::{Value,json};
use crate::{store::{AppState,err},Result,ai,export,domain::markdown};
use serde::{Serialize,Deserialize};

#[derive(Clone,Serialize,Deserialize)]pub struct Integration{#[serde(default)]pub id:String,pub name:String,pub url:String,pub format:String,#[serde(default)]pub token:String}
fn redact(mut v:Value)->Value{v["has_token"]=json!(!v["token"].as_str().unwrap_or("").is_empty());v["token"]=json!("");v}
pub async fn list(State(s):State<AppState>)->Result<Json<Vec<Value>>>{Ok(Json(s.list("integration").await?.into_iter().map(redact).collect()))}
async fn save_inner(s:&AppState,mut v:Integration,existing:Option<&str>)->Result<Value>{
    if v.name.trim().is_empty()||v.name.len()>200{return Err("Укажите название подключения".into())}
    let url=reqwest::Url::parse(&v.url).map_err(|_|"Неверный URL")?;
    if !["http","https"].contains(&url.scheme())||!url.username().is_empty()||url.password().is_some()||url.fragment().is_some()||url.query().is_some(){return Err("Используйте HTTP(S) URL без встроенных credentials и параметров".into())}
    if !["json","multipart"].contains(&v.format.as_str()){return Err("Формат: json или multipart".into())}
    if s.settings().await?.strict_local{ai::local_url(&v.url)?;}
    v.id=existing.map(str::to_string).unwrap_or_else(||uuid::Uuid::new_v4().to_string());
    if v.token.is_empty(){if let Some(old)=s.get("integration",&v.id).await?{v.token=old["token"].as_str().unwrap_or("").into();}}else{v.token=s.encrypt(&v.token)?;}
    let value=serde_json::to_value(&v).map_err(err)?;s.put("integration",&v.id,&value).await?;Ok(redact(value))
}
pub async fn save(State(s):State<AppState>,Json(v):Json<Integration>)->Result<Json<Value>>{Ok(Json(save_inner(&s,v,None).await?))}
pub async fn update(State(s):State<AppState>,Path(id):Path<String>,Json(v):Json<Integration>)->Result<Json<Value>>{if s.get("integration",&id).await?.is_none(){return Err("Подключение не найдено".into())}Ok(Json(save_inner(&s,v,Some(&id)).await?))}
pub async fn remove(State(s):State<AppState>,Path(id):Path<String>)->Result<Json<Value>>{sqlx::query("DELETE FROM records WHERE kind='integration' AND id=?").bind(id).execute(&s.db).await.map_err(err)?;Ok(Json(json!({"ok":true})))}
pub async fn deliveries(State(s):State<AppState>)->Result<Json<Vec<Value>>>{Ok(Json(s.list("delivery").await?))}
pub async fn send(State(s):State<AppState>,Path((id,destination)):Path<(String,String)>)->Result<Json<Value>>{
    let m=s.meeting(&id).await?;if !m.approved{return Err("Сначала проверьте и утвердите протокол".into())}
    let integration:Integration=serde_json::from_value(s.get("integration",&destination).await?.ok_or("Подключение не найдено")?).map_err(err)?;
    if s.settings().await?.strict_local{ai::local_url(&integration.url)?;}
    let key=format!("{}:{}:{}",m.id,m.revision,destination);
    let mut delivery=json!({"id":key,"meeting_id":m.id,"meeting_title":m.title,"revision":m.revision,"destination":destination,"destination_name":integration.name,"status":"sending","created_at":chrono::Utc::now().to_rfc3339(),"error":null});
    let inserted=sqlx::query("INSERT OR IGNORE INTO records(kind,id,body) VALUES('delivery',?,?)").bind(&key).bind(delivery.to_string()).execute(&s.db).await.map_err(err)?.rows_affected();
    if inserted==0{return Err("Эта версия уже отправлялась. Проверьте журнал доставки и получателя.".into())}
    let initial=delivery.clone();tokio::spawn(async move {
        let result:std::result::Result<(),String>=async{
            let mut req=ai::client()?.post(&integration.url).header("Idempotency-Key",&key).timeout(std::time::Duration::from_secs(60));
            if !integration.token.is_empty(){req=req.bearer_auth(s.decrypt(&integration.token)?);}
            if integration.format=="json"{req=req.json(&json!({"schema_version":1,"meeting":m}));}else{
                let doc=export::render_docx(&m.title,&markdown(&m))?;
                req=req.multipart(reqwest::multipart::Form::new().text("metadata",serde_json::to_string(&m).map_err(err)?).part("file",reqwest::multipart::Part::bytes(doc).file_name("protocol.docx").mime_str("application/vnd.openxmlformats-officedocument.wordprocessingml.document").map_err(err)?));
            }
            let r=req.send().await.map_err(|_|"Нет подтверждения доставки. Проверьте СЭД перед повторной отправкой.")?;
            if !r.status().is_success(){return Err(format!("Получатель вернул HTTP {}. Проверьте СЭД: запрос мог быть обработан.",r.status()))}Ok(())
        }.await;
        delivery["status"]=json!(if result.is_ok(){"delivered"}else{"unknown"});if let Err(e)=result{delivery["error"]=json!(e);}let _=s.put("delivery",&key,&delivery).await;
    });Ok(Json(initial))
}
