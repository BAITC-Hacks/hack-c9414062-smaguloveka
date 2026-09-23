use crate::domain::{Meeting, Settings};
use sqlx::{sqlite::{SqliteConnectOptions,SqlitePoolOptions},SqlitePool};
use std::{path::PathBuf,sync::Arc};
use tokio::sync::{Mutex,Semaphore};
use serde_json::{Value,json};
use std::collections::HashMap;
use ring::{aead,rand::{SecureRandom,SystemRandom}};
use base64::{Engine,engine::general_purpose::STANDARD};

#[derive(Clone)] pub struct AppState {
    pub db:SqlitePool, pub dir:PathBuf, pub jobs:Arc<Semaphore>,
    pub downloads:Arc<Mutex<HashMap<String,Value>>>, pub settings_lock:Arc<Mutex<()>>,
    key:Arc<Vec<u8>>,
}
impl AppState {
    pub async fn open(dir:PathBuf)->Result<Self,String> {
        for p in [dir.clone(),dir.join("audio"),dir.join("models")] {tokio::fs::create_dir_all(p).await.map_err(err)?;}
        let db=SqlitePoolOptions::new().max_connections(4).connect_with(SqliteConnectOptions::new().filename(dir.join("hatshy.sqlite")).create_if_missing(true).journal_mode(sqlx::sqlite::SqliteJournalMode::Wal)).await.map_err(err)?;
        sqlx::query("CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL,id TEXT NOT NULL,body TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(kind,id))").execute(&db).await.map_err(err)?;
        let kp=dir.join("secret.key");
        if !kp.exists(){
            let mut key=vec![0;32];SystemRandom::new().fill(&mut key).map_err(|_|"Random generator unavailable")?;
            use std::io::Write;let mut opts=std::fs::OpenOptions::new();opts.write(true).create_new(true);
            #[cfg(unix)] {use std::os::unix::fs::OpenOptionsExt;opts.mode(0o600);}
            let mut f=opts.open(&kp).map_err(err)?;f.write_all(&key).map_err(err)?;
        }
        let key=tokio::fs::read(kp).await.map_err(err)?;if key.len()!=32{return Err("Invalid secret key file".into())}
        let state=Self{db,dir,jobs:Arc::new(Semaphore::new(1)),downloads:Default::default(),settings_lock:Default::default(),key:Arc::new(key)};
        for mut m in state.meetings().await? {if ["queued","transcribing","summarizing"].contains(&m.status.as_str()){m.status="error".into();m.error=Some("Обработка прервана перезапуском. Запустите повторно.".into());state.save_meeting(&mut m).await?;}}
        for mut d in state.list("delivery").await? {if d["status"]=="sending" {d["status"]=json!("unknown");d["error"]=json!("Перезапуск во время отправки. Проверьте получателя перед повтором.");state.put("delivery",d["id"].as_str().unwrap_or_default(),&d).await?;}}
        Ok(state)
    }
    pub async fn get(&self,kind:&str,id:&str)->Result<Option<Value>,String>{
        let row:Option<(String,)>=sqlx::query_as("SELECT body FROM records WHERE kind=? AND id=?").bind(kind).bind(id).fetch_optional(&self.db).await.map_err(err)?;
        row.map(|r|serde_json::from_str(&r.0).map_err(err)).transpose()
    }
    pub async fn list(&self,kind:&str)->Result<Vec<Value>,String>{let rows:Vec<(String,)>=sqlx::query_as("SELECT body FROM records WHERE kind=? ORDER BY rowid DESC").bind(kind).fetch_all(&self.db).await.map_err(err)?;rows.into_iter().map(|r|serde_json::from_str(&r.0).map_err(err)).collect()}
    pub async fn put(&self,kind:&str,id:&str,v:&Value)->Result<(),String>{sqlx::query("INSERT INTO records(kind,id,body) VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body").bind(kind).bind(id).bind(v.to_string()).execute(&self.db).await.map_err(err)?;Ok(())}
    pub async fn meeting(&self,id:&str)->Result<Meeting,String>{serde_json::from_value(self.get("meeting",id).await?.ok_or("Совещание не найдено")?).map_err(err)}
    pub async fn meetings(&self)->Result<Vec<Meeting>,String>{self.list("meeting").await?.into_iter().map(|v|serde_json::from_value(v).map_err(err)).collect()}
    pub async fn insert_meeting(&self,m:&Meeting)->Result<(),String>{sqlx::query("INSERT INTO records(kind,id,body,revision) VALUES('meeting',?,?,?)").bind(&m.id).bind(serde_json::to_string(m).map_err(err)?).bind(m.revision).execute(&self.db).await.map_err(err)?;Ok(())}
    pub async fn save_meeting(&self,m:&mut Meeting)->Result<(),String>{let previous=m.revision;m.revision+=1;let changed=sqlx::query("UPDATE records SET body=?,revision=? WHERE kind='meeting' AND id=? AND revision=?").bind(serde_json::to_string(m).map_err(err)?).bind(m.revision).bind(&m.id).bind(previous).execute(&self.db).await.map_err(err)?.rows_affected();if changed!=1 {m.revision=previous;return Err("Конфликт версии: обновите совещание".into())}Ok(())}
    pub async fn settings(&self)->Result<Settings,String>{let mut s:Settings=self.get("settings","main").await?.map(serde_json::from_value).transpose().map_err(err)?.unwrap_or_default();if !s.openai_key.is_empty(){s.openai_key=self.decrypt(&s.openai_key)?;}Ok(s)}
    pub async fn save_settings(&self,mut s:Settings)->Result<(),String>{if !s.openai_key.is_empty(){s.openai_key=self.encrypt(&s.openai_key)?;}self.put("settings","main",&serde_json::to_value(s).map_err(err)?).await}
    pub fn encrypt(&self,text:&str)->Result<String,String>{let k=aead::LessSafeKey::new(aead::UnboundKey::new(&aead::AES_256_GCM,&self.key).map_err(|_|"Encryption key")?);let mut nonce=[0;12];SystemRandom::new().fill(&mut nonce).map_err(|_|"Random")?;let mut data=text.as_bytes().to_vec();k.seal_in_place_append_tag(aead::Nonce::assume_unique_for_key(nonce),aead::Aad::empty(),&mut data).map_err(|_|"Encryption")?;Ok(STANDARD.encode([nonce.to_vec(),data].concat()))}
    pub fn decrypt(&self,text:&str)->Result<String,String>{let mut data=STANDARD.decode(text).map_err(err)?;if data.len()<28{return Err("Invalid encrypted secret".into())}let nonce:[u8;12]=data[..12].try_into().unwrap();let k=aead::LessSafeKey::new(aead::UnboundKey::new(&aead::AES_256_GCM,&self.key).map_err(|_|"Encryption key")?);let plain=k.open_in_place(aead::Nonce::assume_unique_for_key(nonce),aead::Aad::empty(),&mut data[12..]).map_err(|_|"Cannot decrypt secret")?;String::from_utf8(plain.to_vec()).map_err(err)}
}
pub fn err(e:impl std::fmt::Display)->String{e.to_string()}
