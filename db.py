from sqlalchemy import (
    Column, String, ForeignKey, Enum, JSON, DateTime, Integer
)
from sqlalchemy.orm import relationship, declarative_base
from datetime import datetime, UTC
import uuid

Base = declarative_base()

class Node(Base):
    __tablename__ = "nodes"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False, unique=True)
    uuid = Column(String, nullable=False, unique=True)

    host = Column(String, nullable=False)
    env = Column(String, nullable=False)  # dev / test / prod

    status = Column(String, default="UNKNOWN")
    fail_count = Column(Integer, default=0)
    last_seen = Column(String, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(UTC))

    # relationships
    services = relationship(
        "ServiceInstance",
        back_populates="node",
        cascade="all, delete-orphan"
    )


class ServiceInstance(Base):
    __tablename__ = "service_instances"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    node_id = Column(String, ForeignKey("nodes.id"), nullable=False)

    name = Column(String, nullable=False)        # api, worker
    service_uuid = Column(String, nullable=False)

    repo_url = Column(String, nullable=True)      # e.g. github:org/repo
    flake = Column(String, nullable=True)          # e.g. github:org/repo#api
    commands = Column(JSON, nullable=True)         # e.g. ["run", "migrate"]
    healthcheck_url = Column(String, nullable=True)
    env = Column(String, nullable=False)
    triggered_by = Column(String, nullable=True)

    status = Column(String, default="idle")  # idle, running, failed

    created_at = Column(DateTime, default=lambda: datetime.now(UTC))

    # relationships
    node = relationship("Node", back_populates="services")

    operations = relationship(
        "Operation",
        back_populates="service",
        cascade="all, delete-orphan"
    )

class Operation(Base):
    __tablename__ = "operations"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    service_id = Column(String, ForeignKey("service_instances.id"), nullable=True)

    type = Column(String, nullable=False)  # deploy, teardown, rollback
    status = Column(String, default="pending")  # pending, running, success, failed

    # Service info (denormalized for quick access without joins)
    service_name = Column(String, nullable=True)
    environment = Column(String, nullable=True)
    version = Column(String, nullable=True)
    target_version = Column(String, nullable=True)

    message = Column(String, nullable=True)
    error = Column(String, nullable=True)

    metadata_json = Column("metadata", JSON)

    triggered_by = Column(String, nullable=True)  # Who started this? (e.g. Discord user)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))

    service = relationship("ServiceInstance", back_populates="operations")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DATABASE_URL = "sqlite:///./heimdall.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

from sqlalchemy import text

def init_db():
    Base.metadata.create_all(bind=engine)
    # Simple migration: ensure triggered_by exists
    with engine.connect() as conn:
        try:
            conn.execute(text("SELECT triggered_by FROM operations LIMIT 1"))
        except Exception:
            print("⚠️  Operations table missing 'triggered_by' column. Adding it...")
            try:
                conn.execute(text("ALTER TABLE operations ADD COLUMN triggered_by TEXT"))
                conn.commit()
            except Exception as e:
                print(f"❌ Failed to add column: {e}")
        try:
            conn.execute(text("SELECT triggered_by FROM service_instances LIMIT 1"))
        except Exception:
            print("⚠️  service_instances table missing 'triggered_by' column. Adding it...")
            try:
                conn.execute(text("ALTER TABLE service_instances ADD COLUMN triggered_by TEXT"))
                conn.commit()
            except Exception as e:
                print(f"❌ Failed to add column: {e}")
