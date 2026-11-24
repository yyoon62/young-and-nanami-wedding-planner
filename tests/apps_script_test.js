// Runs Code.gs against fake SpreadsheetApp/ContentService/LockService/Utilities to test request logic.
const fs=require('fs');
const rows={Votes:[["venue","person","vote","note","updated"]],Requests:[["id","person","name","url","note","status","created"]]};
const mkSheet=n=>({appendRow:r=>rows[n].push(r),getDataRange:()=>({getValues:()=>rows[n].map(r=>r.slice())}),
 getRange:(r,c,nr,nc)=>({setValues:v=>{rows[n][r-1]=v[0]},setValue:v=>{rows[n][r-1][c-1]=v}})});
global.SpreadsheetApp={getActiveSpreadsheet:()=>({getSheetByName:n=>rows[n]?mkSheet(n):null,insertSheet:n=>mkSheet(n)})};
global.ContentService={createTextOutput:s=>({s,setMimeType(){return this}}),MimeType:{JSON:1}};
global.LockService={getScriptLock:()=>({waitLock(){},releaseLock(){}})};
global.Utilities={getUuid:()=>"id"+Math.random().toString(36).slice(2,8)};
eval(fs.readFileSync(__dirname+'/../apps-script/Code.gs','utf8')+"\nglobal.doGet=doGet;global.doPost=doPost;");
const post=o=>JSON.parse(doPost({postData:{contents:JSON.stringify(Object.assign({key:"change-me"},o))}}).s);
const get=p=>JSON.parse(doGet({parameter:Object.assign({key:"change-me"},p)}).s);
const t=(n,c)=>console.log(c?"PASS":"FAIL",n);
t("add request",post({action:"request",person:"Nanami",name:"Gardens at Ashland",url:"https://x.com",note:"pretty"}).ok);
t("bad person",!post({action:"request",person:"Eve",name:"X"}).ok);
t("bad url",!post({action:"request",person:"Young",name:"X",url:"javascript:alert(1)"}).ok);
t("name required",!post({action:"request",person:"Young",name:"  "}).ok);
t("wrong key",JSON.parse(doPost({postData:{contents:JSON.stringify({key:"nope",action:"request",person:"Young",name:"Z"})}}).s).ok===false);
post({action:"request",person:"Young",name:"=HYPERLINK(\"http://evil\")"});
t("formula neutralized",rows.Requests[rows.Requests.length-1][2].startsWith("'="));
const list=get({action:"requests"});
t("list has 2",list.length===2);
t("set status",post({action:"setStatus",id:list[0].id,status:"added"}).ok && get({action:"requests"})[0].status==="added");
t("bad status",!post({action:"setStatus",id:list[0].id,status:"hacked"}).ok);
t("vote still works",post({venue:"codman",person:"Young",vote:"love",note:"hi"}).ok && get({}).length===1);
t("vote upsert",post({venue:"codman",person:"Young",vote:"maybe"}).ok && get({}).length===1 && get({})[0].vote==="maybe");
t("wrong key get",!Array.isArray(JSON.parse(doGet({parameter:{key:"x"}}).s)));
