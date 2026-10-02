from .db import get_db, get_db_context

def set_fault(name: str, armed: bool, after_n_calls: int = 0, times: int = 0):
    with get_db_context() as conn:
        with conn:
            conn.execute("""
                INSERT INTO chaos_flags (name, armed, after_n_calls, calls_remaining, times)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    armed=excluded.armed,
                    after_n_calls=excluded.after_n_calls,
                    calls_remaining=excluded.after_n_calls,
                    times=excluded.times
            """, (name, 1 if armed else 0, after_n_calls, after_n_calls, times))

def check_fault(name: str) -> bool:
    with get_db_context() as conn:
        row = conn.execute("SELECT * FROM chaos_flags WHERE name=?", (name,)).fetchone()
        if not row or not row['armed']:
            return False
            
        if row['calls_remaining'] > 0:
            with conn:
                conn.execute("UPDATE chaos_flags SET calls_remaining = calls_remaining - 1 WHERE name=?", (name,))
            return False
            
        if row['times'] > 0:
            with conn:
                conn.execute("UPDATE chaos_flags SET times = times - 1 WHERE name=?", (name,))
                if row['times'] - 1 == 0:
                    conn.execute("UPDATE chaos_flags SET armed = 0 WHERE name=?", (name,))
            return True
            
        return True # if times is 0, it means trigger indefinitely after calls_remaining == 0

def increment_session_requests(session_id: str):
    with get_db_context() as conn:
        with conn:
            conn.execute("UPDATE sessions SET request_count = request_count + 1 WHERE id=?", (session_id,))
            row = conn.execute("SELECT request_count FROM sessions WHERE id=?", (session_id,)).fetchone()
            return row['request_count'] if row else 0