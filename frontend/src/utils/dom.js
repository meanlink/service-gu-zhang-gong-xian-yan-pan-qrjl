export const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const fmt=v=>typeof v==='number'?new Intl.NumberFormat('zh-CN',{maximumFractionDigits:4}).format(v):String(v??'—');
export const date=v=>new Date(v*1000).toLocaleString('zh-CN',{hour12:false});
export function notify(message,error=false){const t=document.querySelector('#toast');t.textContent=message;t.className=error?'show error':'show';setTimeout(()=>t.className='',5000);}
export function duration(seconds){if(!Number.isFinite(seconds))return '—';if(seconds%86400===0)return seconds/86400+' 天';if(seconds%3600===0)return seconds/3600+' 小时';if(seconds%60===0)return seconds/60+' 分钟';return seconds+' 秒';}
