from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from config import DB_PATH

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


class Student(Base):
    __tablename__ = "students"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False, index=True)
    username = Column(String, nullable=True)
    full_name = Column(String, nullable=False)
    group_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    submissions = relationship(
        "Submission",
        back_populates="student",
        order_by="Submission.created_at.desc()",
    )


class Submission(Base):
    __tablename__ = "submissions"

    id = Column(Integer, primary_key=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    kind = Column(String, nullable=False)  # "text" или "voice"
    text_content = Column(Text, nullable=True)
    file_path = Column(String, nullable=True)  # имя файла в MEDIA_DIR
    caption = Column(Text, nullable=True)  # описание задания от студента
    created_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String, default="new")  # "new" или "reviewed"
    feedback_text = Column(Text, nullable=True)
    feedback_at = Column(DateTime, nullable=True)

    student = relationship("Student", back_populates="submissions")


def init_db():
    Base.metadata.create_all(engine)
