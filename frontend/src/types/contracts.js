/** @typedef {{id:number,name:string,source:string,rows:number,checksum:string}} Dataset */
/** @typedef {{id:number,name:string,dataset_id:number,status:string,result:Object,approved:boolean}} AnalysisJob */
/** @typedef {{id:number,name:string,job_id:number,destination:string,next_run:number,enabled:boolean}} ReportSchedule */
/** @typedef {{id:number,report_id:number,due_at:number,status:string,attempts:number,error:string,delivery_id:number|null,completed_at:number|null}} ReportPeriod */
/** @typedef {{id:number,report_id:number,created_at:number,checksum:string,filename:string,destination:string,period_id:number|null,archive:'ready'|'missing'|'corrupt'}} Delivery */
export {};
