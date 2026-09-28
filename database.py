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


class Group(Base):
    """Учебная группа. Список групп ведёт преподаватель в веб-панели —
    студенты в боте выбирают группу из этого списка кнопками."""

    __tablename__ = "groups"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    students = relationship("Student", back_populates="group")


class Student(Base):
    __tablename__ = "students"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False, index=True)
    username = Column(String, nullable=True)
    full_name = Column(String, nullable=False)
    # Устаревшее свободное поле группы — заполнялось до появления таблицы
    # Group, когда студент вводил группу текстом. Оставлено для старых
    # записей и как запасной вариант, если у преподавателя ещё нет ни одной
    # группы в списке.
    group_name = Column(String, nullable=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=True)
    # Заметка преподавателя о студенте — видна только преподавателю,
    # студенту никогда не показывается и не отправляется.
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    group = relationship("Group", back_populates="students")
    submissions = relationship(
        "Submission",
        back_populates="student",
        order_by="Submission.created_at.desc()",
    )

    @property
    def display_group(self) -> str | None:
        """Название группы для показа: сначала таблица Group, иначе
        старое текстовое поле."""
        if self.group is not None:
            return self.group.name
        return self.group_name


class Submission(Base):
    __tablename__ = "submissions"

    id = Column(Integer, primary_key=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    kind = Column(String, nullable=False)  # "text", "voice" или "document"
    text_content = Column(Text, nullable=True)
    # Для одиночного файла старого формата (голосовые всегда используют эти
    # поля). Для файлов с несколькими вложениями используется таблица
    # SubmissionFile ниже, эти поля тогда остаются пустыми.
    file_path = Column(String, nullable=True)
    original_filename = Column(String, nullable=True)
    caption = Column(Text, nullable=True)  # описание задания от студента
    created_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String, default="new")  # "new" или "reviewed"
    feedback_text = Column(Text, nullable=True)
    # Голосовой отзыв преподавателя (файл в MEDIA_DIR, .ogg/opus).
    feedback_voice_path = Column(String, nullable=True)
    feedback_at = Column(DateTime, nullable=True)

    student = relationship("Student", back_populates="submissions")
    files = relationship(
        "SubmissionFile",
        back_populates="submission",
        order_by="SubmissionFile.order_index",
        cascade="all, delete-orphan",
    )


class SubmissionFile(Base):
    """Одно вложение задания. Используется, когда студент присылает
    несколько файлов альбомом за раз — тогда на одно задание (Submission)
    приходится несколько строк здесь."""

    __tablename__ = "submission_files"

    id = Column(Integer, primary_key=True)
    submission_id = Column(Integer, ForeignKey("submissions.id"), nullable=False)
    file_path = Column(String, nullable=False)
    original_filename = Column(String, nullable=True)
    order_index = Column(Integer, default=0)

    submission = relationship("Submission", back_populates="files")


def init_db():
    Base.metadata.create_all(engine)
    _run_light_migrations()


def _run_light_migrations():
    """Добавляет новые колонки/таблицы в уже существующую базу (SQLite),
    если бот обновили поверх старой установки без новых полей."""
    with engine.connect() as conn:
        existing_student_cols = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(students)")
        }
        if "notes" not in existing_student_cols:
            conn.exec_driver_sql("ALTER TABLE students ADD COLUMN notes TEXT")
        if "group_id" not in existing_student_cols:
            conn.exec_driver_sql("ALTER TABLE students ADD COLUMN group_id INTEGER")

        existing_submission_cols = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(submissions)")
        }
        if "original_filename" not in existing_submission_cols:
            conn.exec_driver_sql(
                "ALTER TABLE submissions ADD COLUMN original_filename TEXT"
            )
        if "feedback_voice_path" not in existing_submission_cols:
            conn.exec_driver_sql(
                "ALTER TABLE submissions ADD COLUMN feedback_voice_path TEXT"
            )
        conn.commit()
