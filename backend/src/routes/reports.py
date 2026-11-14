import os
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from src.config.database import get_db
from src.config.settings import ARTIFACT_DIR
from src.models import Report, Delivery, ReportPeriod
from src.schemas.report import ReportInput
from src.middlewares.auth import current_user, admin_only
from src.services import reports
from src.services.audit import record
from src.utils.serialization import report_dict, delivery_dict, period_dict
router=APIRouter()
@router.get('/reports')
def listing(db=Depends(get_db),u=Depends(current_user)):
    return [report_dict(r) for r in db.scalars(select(Report).order_by(Report.id.desc()))]
@router.post('/reports',status_code=201)
def create(data:ReportInput,db=Depends(get_db),u=Depends(admin_only)):
    r=reports.create(db,data); record(db,u.email,'report.schedule',{'id':r.id,'destination':r.destination}); db.commit()
    return report_dict(r)
@router.post('/reports/{id}/toggle')
def toggle(id:int,db=Depends(get_db),u=Depends(admin_only)):
    r=db.get(Report,id)
    if not r:raise HTTPException(404,'分发计划不存在')
    reports.set_enabled(db,r);record(db,u.email,'report.toggle',{'id':id,'enabled':r.enabled});db.commit()
    return report_dict(r)
@router.get('/periods/summary')
def periods_summary(db=Depends(get_db),u=Depends(current_user)):
    rows=db.execute(select(ReportPeriod.status,func.count()).group_by(ReportPeriod.status)).all()
    counts={status:0 for status in reports.PERIOD_STATUSES}
    for status,count in rows:counts[status]=count
    return {'counts':counts,'total':sum(counts.values())}
@router.get('/periods')
def periods(report_id:int|None=None,status:str|None=None,db=Depends(get_db),u=Depends(current_user)):
    def fetch(condition,limit):
        q=select(ReportPeriod).where(condition)
        if report_id is not None:q=q.where(ReportPeriod.report_id==report_id)
        q=q.order_by(ReportPeriod.due_at.desc(),ReportPeriod.id.desc()).limit(limit)
        return list(db.scalars(q))
    if status is not None:
        if status not in reports.PERIOD_STATUSES:raise HTTPException(422,'未知的期次状态')
        rows=fetch(ReportPeriod.status==status,500)
    elif report_id is not None:
        rows=fetch(ReportPeriod.report_id==report_id,500)
    else:
        # 未归档（含失败）始终可见，已归档保留最近 200 期，避免长停机把失败期次挤出页面。
        rows=fetch(ReportPeriod.status!='delivered',500)+fetch(ReportPeriod.status=='delivered',200)
    rows.sort(key=lambda p:(p.due_at,p.id),reverse=True)
    return [period_dict(p) for p in rows]
@router.post('/periods/{id}/retry')
def retry_period(id:int,db=Depends(get_db),u=Depends(admin_only)):
    p=db.get(ReportPeriod,id)
    if not p:raise HTTPException(404,'期次不存在')
    if p.status==reports.PERIOD_DELIVERED:raise HTTPException(409,'该期已归档，无需重试')
    if p.status==reports.PERIOD_RUNNING:raise HTTPException(409,'该期正在处理中，请稍后刷新')
    plan=db.get(Report,p.report_id)
    if not plan or not plan.enabled:raise HTTPException(409,'分发计划已暂停，请先启用后再重试该期')
    reports.retry_period(db,p);record(db,u.email,'report.retry',{'period':id});db.commit()
    return period_dict(p)
@router.get('/deliveries')
def deliveries(db=Depends(get_db),u=Depends(current_user)):
    result=[]
    for d in db.scalars(select(Delivery).order_by(Delivery.id.desc()).limit(100)):
        item=delivery_dict(d);item['period_id']=db.scalar(select(ReportPeriod.id).where(ReportPeriod.delivery_id==d.id));item['archive']=reports.archive_status(d);result.append(item)
    return result
@router.post('/deliveries/{id}/repair')
def repair(id:int,db=Depends(get_db),u=Depends(admin_only)):
    d=db.get(Delivery,id)
    if not d:raise HTTPException(404,'归档不存在')
    reports.repair_delivery(db,d)
    record(db,u.email,'delivery.repair',{'id':id,'checksum':d.checksum});db.commit()
    return delivery_dict(d)
@router.get('/deliveries/{id}/download')
def download(id:int,format:str='csv',db=Depends(get_db),u=Depends(current_user)):
    d=db.get(Delivery,id)
    if not d:raise HTTPException(404,'归档不存在')
    if format not in ('csv','json'):raise HTTPException(422,'支持 CSV 或 JSON')
    try:
        reports.verify_archive(d)
    except reports.ArchiveMissing as exc:
        raise HTTPException(409,f'归档文件缺失：{exc}。请联系负责人修复原归档，无需新建期次。')
    except reports.ArchiveCorrupt as exc:
        raise HTTPException(409,f'归档校验不符：{exc}。请联系负责人修复原归档，无需新建期次。')
    filename=d.filename+('.json' if format=='json' else '')
    p=os.path.join(ARTIFACT_DIR,filename)
    return FileResponse(p,filename=filename,media_type='application/json' if format=='json' else 'text/csv; charset=utf-8')
