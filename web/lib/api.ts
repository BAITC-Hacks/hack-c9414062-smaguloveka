export type Segment={id:number;start:number;end:number;text:string;speaker:string|null};
export type Action={id:string;text:string;assignee:string|null;deadline:string|null;deadline_text:string|null;evidence:number[];status:string};
export type Protocol={summary:string;decisions:string[];questions:string[];actions:Action[]};
export type Meeting={id:string;title:string;date:string|null;created_at:string;language:string;status:string;error:string|null;duration:number;segments:Segment[];protocol:Protocol|null;revision:number;approved:boolean;audio:boolean};
export type Settings={strict_local:boolean;provider:string;asr_model:string;llm_model:string;ollama_url:string;ollama_model:string;openai_model:string;openai_key:string;has_openai_key:boolean;downloads:Record<string,{status:string;error?:string}>};
export type Model={id:string;name:string;kind:string;bytes:number;installed:boolean;download:null|{status:string;received?:number;total?:number;error?:string}};
export type Integration={id:string;name:string;url:string;format:string;token:string;has_token?:boolean};
export type Delivery={id:string;meeting_title:string;destination_name:string;status:string;created_at:string;error:string|null};
export async function api<T>(path:string,method='GET',data?:unknown):Promise<T>{
 const headers:Record<string,string>={'x-hatshy-client':'web'};let body:BodyInit|undefined;
 if(data instanceof FormData)body=data;else if(data!==undefined){headers['content-type']='application/json';body=JSON.stringify(data)}
 const r=await fetch('/api'+path,{method,headers,body,cache:'no-store'});if(!r.ok){const e=await r.json().catch(()=>({error:`Ошибка HTTP ${r.status}`}));throw new Error(e.error||`Ошибка HTTP ${r.status}`)}return r.json();
}
export const statuses:Record<string,string>={uploaded:'Ожидает обработки',queued:'В очереди',transcribing:'Распознавание',summarizing:'Формирование протокола',ready:'Готов к проверке',error:'Нужна помощь'};
export const busy=(m:Meeting)=>['queued','transcribing','summarizing'].includes(m.status);
export const duration=(seconds:number)=>`${Math.floor(seconds/60)}:${Math.floor(seconds%60).toString().padStart(2,'0')}`;
