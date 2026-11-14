import csv
import hashlib
import io
import json
import logging
import os
import time
import uuid
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from src.config.settings import ARTIFACT_DIR
from src.models import Delivery, Job, Report, ReportPeriod

PERIOD_QUEUED = 'queued'
PERIOD_RUNNING = 'running'
PERIOD_DELIVERED = 'delivered'
PERIOD_FAILED = 'failed'
PERIOD_STATUSES = (PERIOD_QUEUED, PERIOD_RUNNING, PERIOD_DELIVERED, PERIOD_FAILED)

# 自动重试只针对瞬时故障；超过次数仍失败就保留原因，等负责人手动重试。
MAX_AUTO_ATTEMPTS = 3

# 一顿停机可能跨过很多期；每轮先补齐一段，下一轮继续，不会从恢复时刻重新计时。
PERIODS_PER_TICK = 5000
PERIODS_PROCESSED_PER_TICK = 200


class ArchiveMissing(Exception):
    """归档文件不存在或没有落盘完成。"""


class ArchiveCorrupt(Exception):
    """归档文件存在，但内容与收件箱记录的摘要不符。"""


def create(db, data):
    j = db.get(Job, data.job_id)
    if not j or j.status != 'completed' or not j.approved:
        raise HTTPException(409, '请先选择已完成且获批准的分析结果')
    now = time.time()
    r = Report(name=data.name, job_id=j.id, destination=data.destination,
               interval_seconds=data.interval_seconds, next_run=round(now + data.delay_seconds, 3),
               enabled=True)
    db.add(r)
    db.flush()
    return r


def set_enabled(db, report):
    """暂停/恢复不补发暂停期间的期次；恢复时从下一应发时点继续，不按恢复时刻重排。"""
    report.enabled = not report.enabled
    now = time.time()
    step = max(int(report.interval_seconds), 1)
    if report.next_run <= now:
        start = report.next_run
        missed = int((now - start) // step) + 1
        report.next_run = round(start + missed * step, 3)
    return report


def _safe_cell(value):
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    return "'" + text if text.startswith(('=', '+', '-', '@', '\t', '\r')) else text


def _csv_bytes(job):
    out = io.StringIO()
    out.write('﻿')
    table = (job.result or {}).get('table', {})
    columns = table.get('columns', [])
    writer = csv.writer(out)
    writer.writerow([_safe_cell(c['label']) for c in columns])
    for row in table.get('rows', []):
        writer.writerow([_safe_cell(row.get(c['key'], '')) for c in columns])
    return out.getvalue().encode('utf-8')


def _json_bytes(job, report, period_id, due_at, checksum):
    return json.dumps({
        'job': job.name,
        'params': job.params,
        'result': job.result,
        'checksum': checksum,
        'report': {'id': report.id, 'name': report.name, 'destination': report.destination},
        'period': {'id': period_id, 'due_at': due_at},
    }, ensure_ascii=False, allow_nan=False).encode('utf-8')


def _path(filename):
    return os.path.join(ARTIFACT_DIR, filename)


def _write_atomic(path, data):
    """先写同目录临时文件并落盘，再原子替换；崩溃只会留下可清理的临时文件。"""
    directory = os.path.dirname(path) or ARTIFACT_DIR
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f'.{os.path.basename(path)}.{uuid.uuid4().hex}.tmp')
    with open(tmp, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _hash_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_archive(job, report, period_id, due_at, filename):
    """生成一份完整归档（CSV+JSON），返回 CSV 的 SHA256。调用方只在文件齐备后写入收件箱。"""
    csv_data = _csv_bytes(job)
    digest = hashlib.sha256(csv_data).hexdigest()
    json_data = _json_bytes(job, report, period_id, due_at, digest)
    csv_path = _path(filename)
    json_path = _path(filename + '.json')
    try:
        _write_atomic(csv_path, csv_data)
        _write_atomic(json_path, json_data)
        # 落盘后回读校验，避免把半份或损坏文件当作归档。
        if not os.path.isfile(csv_path) or _hash_file(csv_path) != digest:
            raise ArchiveCorrupt('归档 CSV 写入后校验失败')
        if not os.path.isfile(json_path):
            raise ArchiveMissing('归档 JSON 未写入')
        try:
            payload = json.loads(Path(json_path).read_text(encoding='utf-8'))
        except (ValueError, OSError) as exc:
            raise ArchiveCorrupt('归档 JSON 无法解析') from exc
        if payload.get('checksum') != digest:
            raise ArchiveCorrupt('归档 JSON 摘要与 CSV 不一致')
    except Exception:
        # 本次没有生成完整归档：清掉可能残留的半份文件，收件箱不会登记这一期。
        for path in (csv_path, json_path):
            try:
                os.remove(path)
            except OSError:
                pass
        raise
    return digest


def verify_archive(delivery):
    """下载前校验 CSV 与 JSON 都存在且摘要一致。"""
    csv_path = _path(delivery.filename)
    json_path = _path(delivery.filename + '.json')
    if not os.path.isfile(csv_path) or not os.path.isfile(json_path):
        raise ArchiveMissing('CSV 或 JSON 文件不存在')
    if _hash_file(csv_path) != delivery.checksum:
        raise ArchiveCorrupt('CSV 的 SHA256 与收件箱记录不一致')
    try:
        payload = json.loads(Path(json_path).read_text(encoding='utf-8'))
    except (ValueError, OSError) as exc:
        raise ArchiveCorrupt('JSON 无法解析') from exc
    if payload.get('checksum') != delivery.checksum:
        raise ArchiveCorrupt('JSON 摘要与收件箱记录不一致')


def archive_status(delivery):
    try:
        verify_archive(delivery)
    except ArchiveMissing:
        return 'missing'
    except ArchiveCorrupt:
        return 'corrupt'
    return 'ready'


def generate_periods(db, now, limit=PERIODS_PER_TICK):
    """按计划的应发时点补建期次；停机跨过的每一期都会保留，不按恢复时刻重排。

    due_at = 计划起点 + k×周期，只用乘法计算，保证跨轮次、跨 worker 的浮点值一致。
    (report_id, due_at) 唯一约束保证并发 worker 不会为同一期留下两条记录。
    """
    generated = 0
    rows = list(db.scalars(select(Report).where(
        Report.enabled == True, Report.next_run <= now).order_by(Report.id).with_for_update()))
    for report in rows:
        if generated >= limit:
            break
        step = max(int(report.interval_seconds), 1)
        start = report.next_run
        existing = set(db.scalars(select(ReportPeriod.due_at).where(
            ReportPeriod.report_id == report.id,
            ReportPeriod.due_at >= start, ReportPeriod.due_at <= now)))
        index = 0
        while generated < limit:
            # 四舍五入到毫秒：并发 worker 各自算出的同一应发时点会得到同一个值。
            due = round(start + index * step, 3)
            if due > now:
                break
            if due not in existing:
                try:
                    with db.begin_nested():
                        db.add(ReportPeriod(report_id=report.id, due_at=due,
                                            status=PERIOD_QUEUED, attempts=0, error='',
                                            created_at=now, updated_at=now))
                        db.flush()
                    generated += 1
                except IntegrityError:
                    pass  # 另一个 worker 已创建同一期
            index += 1
        report.next_run = round(start + index * step, 3)
    db.commit()
    return generated


def _failure(db, period_id, message):
    try:
        db.rollback()
        db.execute(update(ReportPeriod).where(
            ReportPeriod.id == period_id, ReportPeriod.status == PERIOD_RUNNING
        ).values(status=PERIOD_FAILED, error=str(message)[:1000], updated_at=time.time()))
        db.commit()
    except Exception:
        logging.exception('Could not mark report period failed')
        db.rollback()


def execute_period(db, period_id):
    """领取并处理一期。原子领取保证并发执行同一期时只产生一份归档。"""
    now = time.time()
    claimed = db.execute(update(ReportPeriod).where(
        ReportPeriod.id == period_id,
        ReportPeriod.status.in_((PERIOD_QUEUED, PERIOD_FAILED)),
    ).values(status=PERIOD_RUNNING, attempts=ReportPeriod.attempts + 1,
             error='', updated_at=now))
    db.commit()
    if claimed.rowcount != 1:
        return None
    period = db.get(ReportPeriod, period_id)
    if period is None:
        return None
    db.refresh(period)
    report = db.get(Report, period.report_id)
    job = db.get(Job, report.job_id) if report else None
    try:
        if not report:
            raise RuntimeError('分发计划不存在')
        if not report.enabled:
            raise RuntimeError('分发计划已暂停')
        if not job or job.status != 'completed':
            raise RuntimeError('批准的分析结果不存在或未完成')
        delivery = db.get(Delivery, period.delivery_id) if period.delivery_id else None
        filename = delivery.filename if delivery else f'report-{report.id}-period-{period.id}.csv'
        digest = write_archive(job, report, period.id, period.due_at, filename)
        now = time.time()
        if delivery is None:
            delivery = Delivery(report_id=report.id, created_at=now, checksum=digest,
                                filename=filename, destination=report.destination)
            db.add(delivery)
            db.flush()
            period.delivery_id = delivery.id
        else:
            delivery.checksum = digest
            delivery.destination = report.destination
        period.status = PERIOD_DELIVERED
        period.error = ''
        period.completed_at = now
        period.updated_at = now
        db.commit()
        return period.id, delivery.id
    except Exception as exc:
        logging.exception('Report period failed')
        _failure(db, period_id, f'{type(exc).__name__}: {exc}')
        return None


def process_due(db, now, limit=PERIODS_PROCESSED_PER_TICK):
    """处理到期且待处理的期次。暂停的计划不再投递；瞬时失败短暂退避后自动重试。"""
    outcomes = []
    candidates = list(db.scalars(select(ReportPeriod).join(Report, Report.id == ReportPeriod.report_id).where(
        Report.enabled == True,
        ReportPeriod.due_at <= now,
        (ReportPeriod.status == PERIOD_QUEUED)
        | ((ReportPeriod.status == PERIOD_FAILED) & (ReportPeriod.attempts < MAX_AUTO_ATTEMPTS)),
    ).order_by(ReportPeriod.due_at, ReportPeriod.id).limit(limit)))
    for period in candidates:
        if period.status == PERIOD_FAILED and period.updated_at + min(5 * period.attempts, 60) > now:
            continue
        outcome = execute_period(db, period.id)
        if outcome:
            outcomes.append(outcome)
    return outcomes


def recover_interrupted(db):
    """worker 重启时把崩溃遗留的处理中状态放回队列；已完成的期次不会重做。"""
    now = time.time()
    count = 0
    for period in db.scalars(select(ReportPeriod).where(ReportPeriod.status == PERIOD_RUNNING)):
        period.status = PERIOD_QUEUED
        period.error = '上次处理中断，已重新排队'
        period.updated_at = now
        count += 1
    db.commit()
    return count


def retry_period(db, period):
    if period.status == PERIOD_DELIVERED:
        raise HTTPException(409, '该期已归档，如需重建请修复原归档')
    if period.status == PERIOD_RUNNING:
        raise HTTPException(409, '该期正在处理，请稍后刷新')
    period.status = PERIOD_QUEUED
    period.error = ''
    period.attempts = 0
    period.updated_at = time.time()
    return period


def repair_delivery(db, delivery):
    """按原期次重写文件，只修复原归档，不新增期次。"""
    report = db.get(Report, delivery.report_id)
    if not report:
        raise HTTPException(409, '原分发计划不存在，无法修复')
    job = db.get(Job, report.job_id)
    if not job or job.status != 'completed':
        raise HTTPException(409, '原分析结果不存在，无法修复')
    period = db.scalar(select(ReportPeriod).where(ReportPeriod.delivery_id == delivery.id))
    try:
        digest = write_archive(job, report, period.id if period else None,
                               period.due_at if period else delivery.created_at, delivery.filename)
    except (ArchiveMissing, ArchiveCorrupt) as exc:
        raise HTTPException(409, f'修复失败：{exc}') from exc
    delivery.checksum = digest
    if period:
        period.status = PERIOD_DELIVERED
        period.error = ''
        period.completed_at = period.completed_at or time.time()
        period.updated_at = time.time()
    db.commit()
    return delivery


def cleanup_temps(max_age=3600):
    """清理崩溃遗留的临时文件；收件箱只认最终文件名。"""
    removed = 0
    directory = Path(ARTIFACT_DIR)
    if not directory.exists():
        return removed
    cutoff = time.time() - max_age
    for entry in directory.glob('.*.tmp'):
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            pass
    return removed
