use std::path::PathBuf;
#[tokio::main]
async fn main()->Result<(),Box<dyn std::error::Error>>{
    let dir=PathBuf::from(std::env::var("HATSHY_DATA_DIR").unwrap_or_else(|_|".hatshy".into()));
    let state=hatshy_server::store::AppState::open(dir).await?;
    let web=PathBuf::from(std::env::var("HATSHY_WEB_DIR").unwrap_or_else(|_|"web/out".into()));
    let bind=std::env::var("HATSHY_BIND").unwrap_or_else(|_|"127.0.0.1:8080".into());
    let listener=tokio::net::TcpListener::bind(&bind).await?;
    eprintln!("AI Hatshy: http://localhost:8080");
    axum::serve(listener,hatshy_server::router(state,web)).await?;Ok(())
}
