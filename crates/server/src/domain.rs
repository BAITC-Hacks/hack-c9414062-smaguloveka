use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Segment { pub id: usize, pub start: f64, pub end: f64, pub text: String, pub speaker: Option<String> }
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Action {
    pub id: String, pub text: String, pub assignee: Option<String>, pub deadline: Option<String>,
    pub deadline_text: Option<String>, pub evidence: Vec<usize>, pub status: String,
}
#[derive(Clone, Debug, Serialize, Deserialize, Default)]
pub struct Protocol { pub summary: String, pub decisions: Vec<String>, pub actions: Vec<Action>, pub questions: Vec<String> }
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Meeting {
    pub id: String, pub title: String, pub date: Option<String>, pub created_at: String,
    pub language: String, pub status: String, pub error: Option<String>, pub duration: f64,
    pub segments: Vec<Segment>, pub protocol: Option<Protocol>, pub revision: i64,
    pub audio: bool, pub approved: bool,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    pub strict_local: bool, pub provider: String, pub asr_model: String,
    pub llm_model: String, pub ollama_url: String, pub ollama_model: String,
    pub openai_model: String, pub openai_key: String,
}
impl Default for Settings { fn default()->Self { Self {
    strict_local:true, provider:"ollama".into(), asr_model:"whisper-small".into(),
    llm_model:"qwen-3b".into(), ollama_url:"http://127.0.0.1:11434".into(),
    ollama_model:"qwen2.5:3b".into(), openai_model:"gpt-4o-mini".into(), openai_key:String::new(),
} } }

pub fn validate_protocol(p: &mut Protocol, segments:&[Segment])->Result<(),String> {
    if p.summary.trim().is_empty() {return Err("Модель вернула пустое саммари".into())}
    for action in &mut p.actions {
        if action.text.trim().is_empty() || action.evidence.is_empty() { return Err("Поручение без текста или ссылки на реплику".into()); }
        if action.evidence.iter().any(|id| !segments.iter().any(|s|s.id==*id)) { return Err("Модель сослалась на несуществующую реплику".into()); }
        if let Some(date)=&action.deadline {chrono::NaiveDate::parse_from_str(date,"%Y-%m-%d").map_err(|_|"Некорректный срок поручения")?;}
        action.id=uuid::Uuid::new_v4().to_string(); action.status="todo".into();
    }
    Ok(())
}

pub fn markdown(m:&Meeting)->String {
    let mut s=format!("# {}\n\nДата: {}\nСтатус: {}\n\n",m.title,m.date.as_deref().unwrap_or("не указана"),if m.approved {"Утверждён"}else{"Черновик — требует проверки"});
    if let Some(p)=&m.protocol {
        s.push_str(&format!("## Саммари\n{}\n\n## Решения\n",p.summary));
        for d in &p.decisions {s.push_str(&format!("- {d}\n"));}
        s.push_str("\n## Поручения\n");
        for a in &p.actions {s.push_str(&format!("- {} — Ответственный: {}; срок: {}. Статус: {}. Реплики: {:?}\n",a.text,a.assignee.as_deref().unwrap_or("уточнить"),a.deadline.as_deref().or(a.deadline_text.as_deref()).unwrap_or("уточнить"),a.status,a.evidence));}
        s.push_str("\n## Требует уточнения\n");for q in &p.questions {s.push_str(&format!("- {q}\n"));}
    }
    s.push_str("\n## Транскрипт\n");for seg in &m.segments{s.push_str(&format!("[{} · {:.1}–{:.1}] {}: {}\n\n",seg.id,seg.start,seg.end,seg.speaker.as_deref().unwrap_or("Говорящий не определён"),seg.text));} s
}

#[cfg(test)] mod tests {
    use super::*;
    #[test] fn rejects_invented_evidence() {
        let mut p=Protocol{summary:"Итоги".into(),actions:vec![Action{id:"".into(),text:"Подготовить отчёт".into(),assignee:None,deadline:None,deadline_text:None,evidence:vec![99],status:"".into()}],..Default::default()};
        assert!(validate_protocol(&mut p,&[]).is_err());
    }
}
