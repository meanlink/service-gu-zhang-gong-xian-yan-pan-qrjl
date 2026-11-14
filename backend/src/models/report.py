from sqlalchemy import String, Double, ForeignKey, Boolean, Integer, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class Report(Base):
    __tablename__ = 'reports'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    job_id: Mapped[int] = mapped_column(ForeignKey('jobs.id'))
    destination: Mapped[str] = mapped_column(String(120))
    interval_seconds: Mapped[int]
    next_run: Mapped[float] = mapped_column(Double)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ReportPeriod(Base):
    """一期一条：应发时点是计划时间，不会因为 worker 重启而重新计时。"""
    __tablename__ = 'report_periods'
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey('reports.id'))
    due_at: Mapped[float] = mapped_column(Double)
    status: Mapped[str] = mapped_column(String(20), default='queued')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(String(1000), default='')
    delivery_id: Mapped[int | None] = mapped_column(ForeignKey('deliveries.id'), nullable=True)
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)
    completed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    __table_args__ = (
        UniqueConstraint('report_id', 'due_at', name='uq_report_period_due'),
        Index('ix_report_period_status_due', 'status', 'due_at'),
    )


class Delivery(Base):
    """本地收件箱记录。只有 CSV 与 JSON 都已落盘并通过校验后才创建。"""
    __tablename__ = 'deliveries'
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey('reports.id'))
    created_at: Mapped[float] = mapped_column(Double)
    checksum: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(160))
    destination: Mapped[str] = mapped_column(String(120))
