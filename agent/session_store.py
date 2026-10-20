"""Wave 12 — 프로세스 밖(SQLite 파일)에 두는 세션 저장소.

서버가 재시작돼도(새 프로세스, 새 인스턴스) 같은 session.db 파일을 다시 열므로
대화 이력이 유지된다. in-memory dict였다면 재시작 즉시 소실됐을 상태다.
"""
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "session.db"


class SqliteSessionStore:
    """
    대화방(thread_id)별 메시지 이력을 SQLite 파일에 저장하는 세션 저장소

    Attributes:
        conn:  session.db 연결. 요청 스레드가 달라도 같은 연결을 쓰도록 check_same_thread=False
    """

    def __init__(self, db_path: Path = DB_PATH):
        """
        DB 파일을 열고 messages 테이블이 없으면 생성
        Args:
            db_path:  세션 DB 파일 경로 (기본: 프로젝트 루트의 session.db)
        """
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            " thread_id TEXT, role TEXT, content TEXT, created TEXT)"
        )
        self.conn.commit()

    def load(self, thread_id: str) -> list[dict]:
        """
        대화방의 메시지 이력을 저장 순서대로 조회

        별도 순번 컬럼 없이 rowid 정렬로 삽입 순서를 보장.
        Args:
            thread_id:  대화방 ID
        Returns:
            {"role", "content"} 딕셔너리 목록. 이력이 없으면 빈 목록
        """
        rows = self.conn.execute(
            "SELECT role, content FROM messages WHERE thread_id=? ORDER BY rowid",
            (thread_id,),
        ).fetchall()
        return [{"role": r, "content": c} for r, c in rows]

    def append(self, thread_id: str, role: str, content: str) -> None:
        """
        메시지 한 건을 추가하고 바로 커밋
        Args:
            thread_id:  대화방 ID
            role:  발화 주체 (user / assistant 등)
            content:  메시지 본문
        """
        self.conn.execute(
            "INSERT INTO messages VALUES (?,?,?,?)",
            (thread_id, role, content, datetime.now().isoformat()),
        )
        self.conn.commit()

    def delete(self, thread_id: str) -> None:
        """
        대화방의 메시지 이력 전체 삭제
        Args:
            thread_id:  대화방 ID
        """
        self.conn.execute("DELETE FROM messages WHERE thread_id=?", (thread_id,))
        self.conn.commit()

    def close(self) -> None:
        """
        DB 연결 종료
        """
        self.conn.close()
