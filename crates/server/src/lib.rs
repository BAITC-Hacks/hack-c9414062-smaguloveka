pub mod domain;
pub mod store;
mod ai;
mod models;
mod export;
mod integrations;
mod routes;
use axum::{Router,Json,routing::get,response::{IntoResponse,Response},http::StatusCode};
use serde_json::json;
pub struct ApiError(pub String);
impl From<String> for ApiError {fn from(s:String)->Self{Self(s)}}
impl From<&str> for ApiError {fn from(s:&str)->Self{Self(s.into())}}
impl IntoResponse for ApiError {fn into_response(self)->Response{let code=if self.0.contains("Конфликт"){StatusCode::CONFLICT}else{StatusCode::BAD_REQUEST};(code,Json(json!({"error":self.0}))).into_response()}}
pub type Result<T>=std::result::Result<T,ApiError>;
pub fn app()->Router{Router::new().route("/api/health",get(||async{Json(json!({"status":"ok","app":"AI Hatshy"}))}))}
pub use routes::router;
