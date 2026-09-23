use crate::store::{AppState,err};
use serde::Serialize;
use serde_json::{Value,json};
use futures_util::StreamExt;
use tokio::io::AsyncWriteExt;
use sha2::{Digest,Sha256};

#[derive(Clone,Serialize)] pub struct Model {pub id:&'static str,pub name:&'static str,pub kind:&'static str,pub filename:&'static str,pub bytes:u64,pub sha256:&'static str,pub url:&'static str}
pub fn catalog()->Vec<Model>{vec![
    Model{id:"whisper-small",name:"Whisper Small · RU / KK / MIX",kind:"asr",filename:"ggml-small.bin",bytes:487601967,sha256:"1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b",url:"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin"},
    Model{id:"qwen-3b",name:"Qwen 2.5 Instruct 3B · Q4",kind:"llm",filename:"qwen2.5-3b-instruct-q4_k_m.gguf",bytes:2104932768,sha256:"626b4a6678b86442240e33df819e00132d3ba7dddfe1cdc4fbb18e0a9615c62d",url:"https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf"},
]}
pub fn find(id:&str)->Result<Model,String>{catalog().into_iter().find(|m|m.id==id).ok_or("Модель отсутствует в каталоге".into())}
pub async fn list(s:&AppState)->Vec<Value>{let d=s.downloads.lock().await;catalog().into_iter().map(|m|{let mut v=serde_json::to_value(&m).unwrap();v["installed"]=json!(s.dir.join("models").join(m.filename).is_file());v["download"]=d.get(m.id).cloned().unwrap_or(Value::Null);v}).collect()}
pub async fn start(s:AppState,id:String)->Result<(),String>{let m=find(&id)?;{
    let mut d=s.downloads.lock().await;if d.get(&id).is_some_and(|v|v["status"]=="downloading"){return Err("Уже скачивается".into())}if s.dir.join("models").join(m.filename).is_file(){return Ok(())}d.insert(id.clone(),json!({"status":"downloading","received":0,"total":m.bytes}));
}tokio::spawn(async move {let result=download(&s,&m).await;let mut d=s.downloads.lock().await;d.insert(id,match result{Ok(_)=>json!({"status":"installed","received":m.bytes,"total":m.bytes}),Err(e)=>json!({"status":"error","error":e})});});Ok(())}
async fn download(s:&AppState,m:&Model)->Result<(),String>{
    let temp=s.dir.join("models").join(format!("{}.part",m.filename));
    let result=async {
        let response=reqwest::Client::builder().connect_timeout(std::time::Duration::from_secs(30)).timeout(std::time::Duration::from_secs(7200)).build().map_err(err)?.get(m.url).send().await.map_err(|_|"Не удалось связаться с каталогом моделей")?.error_for_status().map_err(|e|format!("Скачивание: {}",e.status().unwrap_or_default()))?;
        let mut stream=response.bytes_stream();let mut f=tokio::fs::File::create(&temp).await.map_err(err)?;let mut hasher=Sha256::new();let mut received=0u64;
        while let Some(chunk)=stream.next().await{let chunk=chunk.map_err(|_|"Скачивание прервано")?;received+=chunk.len() as u64;if received>m.bytes{return Err("Размер модели превышает каталог".into())}f.write_all(&chunk).await.map_err(err)?;hasher.update(&chunk);s.downloads.lock().await.insert(m.id.into(),json!({"status":"downloading","received":received,"total":m.bytes}));}
        f.sync_all().await.map_err(err)?;drop(f);if received!=m.bytes||hex::encode(hasher.finalize())!=m.sha256{return Err("Контрольная сумма модели не совпала".into())}
        tokio::fs::rename(&temp,s.dir.join("models").join(m.filename)).await.map_err(err)?;Ok(())
    }.await;if result.is_err(){let _=tokio::fs::remove_file(temp).await;}result
}
