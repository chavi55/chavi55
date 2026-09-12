# -*- coding: utf-8 -*-
"""
퀴즈 라이브! — Kahoot! 스타일 실시간 퀴즈 서버 (파이썬 표준 라이브러리만 사용)
- 진행자 화면 : http://localhost:8000/host
- 참가자 접속 : http://<내부IP>:8000/play  (같은 Wi-Fi/네트워크)
- 포트 변경   : py -3 server.py --port 9000
"""
import argparse
import hashlib
import json
import os
import random
import secrets
import socket
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs, unquote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
QUESTIONS_PATH = os.path.join(BASE_DIR, "questions.json")
PORT = 8000

QUIZ = {"title": "퀴즈", "questions": []}

GAME_LOCK = threading.Lock()
GAME = {
    "pin": "",
    "phase": "idle",          # idle → lobby → question → reveal → scoreboard → final
    "q_index": -1,
    "phase_started": 0.0,
    "phase_ends": 0.0,
    "players": {},            # 닉네임 → {score, answered, choice, gained, joined_at, last_seen}
    "participants": [],       # 현재 문제 시작 시점의 참가자 목록
    "game_questions": [],     # 게임 시작 시점의 문제 스냅샷
    "last_result": None,
}


def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def log(line):
    print(time.strftime("[%H:%M:%S] ") + line, flush=True)


# ---------- 문제 관리 ----------

def validate_quiz(data):
    if not isinstance(data, dict):
        raise ValueError("형식이 올바르지 않습니다")
    title = str(data.get("title", "")).strip()[:60] or "퀴즈"
    qs = data.get("questions")
    if not isinstance(qs, list) or not qs:
        raise ValueError("문제가 한 개 이상 필요합니다")
    out = []
    for q in qs[:200]:
        text = str(q.get("q", "")).strip()[:300]
        choices = [str(c).strip()[:80] for c in q.get("choices", [])][:4]
        ans = int(q.get("answer", 0))
        t = int(q.get("time", 20))
        if not text:
            raise ValueError("내용이 비어 있는 문제가 있습니다")
        if len(choices) < 4 or any(not c for c in choices):
            raise ValueError("선택지가 비어 있는 문제가 있습니다: " + text[:20])
        if not 0 <= ans <= 3:
            raise ValueError("정답 번호가 잘못된 문제가 있습니다")
        out.append({"q": text, "choices": choices, "answer": ans, "time": max(5, min(300, t))})
    return {"title": title, "questions": out}


def load_questions():
    global QUIZ
    try:
        with open(QUESTIONS_PATH, "r", encoding="utf-8-sig") as f:
            QUIZ = validate_quiz(json.load(f))
    except Exception as e:
        print(f"[!] questions.json 로드 실패: {e}")
        QUIZ = {"title": "퀴즈", "questions": []}


def save_questions(data):
    cleaned = validate_quiz(data)
    tmp = QUESTIONS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)
    os.replace(tmp, QUESTIONS_PATH)
    global QUIZ
    QUIZ = cleaned
    return len(cleaned["questions"])


# ---------- 게임 로직 ----------

def rank_list_locked():
    items = sorted(GAME["players"].items(), key=lambda kv: (-kv[1]["score"], kv[1]["joined_at"]))
    return [{"name": n, "score": p["score"]} for n, p in items]


def begin_question_locked():
    q = GAME["game_questions"][GAME["q_index"]]
    now = time.time()
    GAME["phase"] = "question"
    GAME["phase_started"] = now
    GAME["phase_ends"] = now + max(5, min(300, int(q.get("time", 20))))
    GAME["participants"] = list(GAME["players"].keys())
    GAME["last_result"] = None
    for p in GAME["players"].values():
        p["answered"] = False
        p["choice"] = None
        p["gained"] = 0


def do_reveal_locked():
    q = GAME["game_questions"][GAME["q_index"]]
    counts = [0, 0, 0, 0]
    for p in GAME["players"].values():
        if p["answered"] and isinstance(p["choice"], int) and 0 <= p["choice"] < 4:
            counts[p["choice"]] += 1
    GAME["last_result"] = {"counts": counts, "correct": q["answer"], "total": sum(counts)}
    GAME["phase"] = "reveal"


def tick_locked():
    """폴링 시점마다 상태를 갱신: 시간 초과/전원 응답 시 정답 공개, 로비·종료 후 미접속자 정리"""
    g = GAME
    if g["phase"] == "question":
        now = time.time()
        # 나간(정리된) 참가자는 조기 공개 판정에서 제외
        present = [n for n in g["participants"] if n in g["players"]]
        all_answered = bool(present) and all(g["players"][n].get("answered") for n in present)
        if now >= g["phase_ends"] or all_answered:
            do_reveal_locked()
    elif g["phase"] in ("lobby", "final"):
        # 게임 진행 중에는 참가자를 정리하지 않는다(복귀 시 이어서 참여 가능)
        cutoff = time.time() - 120
        for n in [n for n, p in g["players"].items() if p["last_seen"] < cutoff]:
            del g["players"][n]


def host_state():
    with GAME_LOCK:
        tick_locked()
        g = GAME
        qs = g["game_questions"]
        st = {
            "phase": g["phase"], "pin": g["pin"], "title": QUIZ["title"],
            "q_total": len(qs),
            "q_index": g["q_index"] + 1 if g["q_index"] >= 0 else 0,
            "players": rank_list_locked(), "players_count": len(g["players"]),
            "lan_ip": get_lan_ip(), "port": PORT,
        }
        if g["phase"] in ("question", "reveal", "scoreboard") and 0 <= g["q_index"] < len(qs):
            q = qs[g["q_index"]]
            st["question"] = {"q": q["q"], "choices": q["choices"], "time": q["time"]}
            st["answered_count"] = sum(1 for p in g["players"].values() if p["answered"])
            st["participants_count"] = len(g["participants"])
            if g["phase"] == "question":
                st["time_left"] = max(0, int(g["phase_ends"] - time.time() + 0.999))
            else:
                st["time_left"] = 0
                st["result"] = g["last_result"]
                st["question"]["answer"] = q["answer"]
        if g["phase"] == "final":
            st["final"] = rank_list_locked()
        return st


def play_state(name):
    with GAME_LOCK:
        tick_locked()
        g = GAME
        p = g["players"].get(name)
        if not p:
            return {"joined": False, "phase": g["phase"], "pin": g["pin"],
                    "players_count": len(g["players"])}
        p["last_seen"] = time.time()
        st = {
            "joined": True, "phase": g["phase"], "pin": g["pin"],
            "q_index": g["q_index"] + 1 if g["q_index"] >= 0 else 0,
            "q_total": len(g["game_questions"]),
            "time_left": max(0, int(g["phase_ends"] - time.time() + 0.999)) if g["phase"] == "question" else 0,
            "players_count": len(g["players"]),
        }
        my = {"answered": p["answered"], "choice": p["choice"],
              "gained": p["gained"], "score": p["score"]}
        if g["phase"] == "question" and 0 <= g["q_index"] < len(g["game_questions"]):
            st["choices"] = g["game_questions"][g["q_index"]]["choices"]
        if g["phase"] in ("reveal", "scoreboard") and 0 <= g["q_index"] < len(g["game_questions"]):
            q = g["game_questions"][g["q_index"]]
            my["correct"] = (p["choice"] == q["answer"]) if p["answered"] else None
            my["answer"] = q["answer"]
        for i, r in enumerate(rank_list_locked()):
            if r["name"] == name:
                my["rank"] = i + 1
                break
        st["my"] = my
        if g["phase"] == "final":
            st["final"] = rank_list_locked()
        return st


def join(pin, name):
    with GAME_LOCK:
        tick_locked()
        g = GAME
        if not g["pin"]:
            return "아직 게임이 준비되지 않았어요. 잠시 후 다시 시도해 주세요."
        if str(pin) != g["pin"]:
            return "PIN 번호가 틀렸어요."
        if g["phase"] != "lobby":
            return "게임이 진행 중이에요. 다음 게임 때 입장해 주세요."
        name = str(name).strip()[:12]
        if not name:
            return "닉네임을 입력해 주세요."
        if name in g["players"]:
            # 같은 PIN이면 재입장(새로고침 등)으로 처리
            g["players"][name]["last_seen"] = time.time()
            log(f"[재입장] {name}")
            return None
        now = time.time()
        g["players"][name] = {"score": 0, "answered": False, "choice": None,
                              "gained": 0, "joined_at": now, "last_seen": now}
        log(f"[입장] {name} (현재 {len(g['players'])}명)")
        return None


def submit_answer(name, choice):
    with GAME_LOCK:
        tick_locked()
        g = GAME
        p = g["players"].get(name)
        if g["phase"] != "question" or not p:
            return "지금은 답을 제출할 수 없어요."
        if p["answered"]:
            return None  # 중복 제출은 무시
        if not isinstance(choice, int) or not 0 <= choice < 4:
            return "잘못된 답이에요."
        q = g["game_questions"][g["q_index"]]
        p["answered"] = True
        p["choice"] = choice
        if choice != q["answer"]:
            p["gained"] = 0
            return None
        limit = max(5, min(300, int(q.get("time", 20))))
        elapsed = max(0.0, time.time() - g["phase_started"])
        pts = int(1000 * max(0.0, 1 - (elapsed / limit) / 2))
        p["gained"] = pts
        p["score"] += pts
        return None


def host_action(action, name=""):
    with GAME_LOCK:
        tick_locked()
        g = GAME
        if action == "new_game":
            g["pin"] = str(random.randint(100000, 999999))
            g["game_questions"] = [dict(q) for q in QUIZ["questions"]]
            now = time.time()
            g["players"] = {n: {"score": 0, "answered": False, "choice": None, "gained": 0,
                                "joined_at": p.get("joined_at", now), "last_seen": now}
                            for n, p in g["players"].items()}
            g["participants"] = []
            g["q_index"] = -1
            g["last_result"] = None
            g["phase"] = "lobby"
            log(f"[게임] 새 게임 생성 PIN={g['pin']} ({len(g['game_questions'])}문제)")
            return None
        if action == "start":
            if g["phase"] != "lobby":
                return "로비에서만 시작할 수 있어요."
            if not g["game_questions"]:
                return "문제가 없어요. 문제 관리에서 문제를 추가해 주세요."
            g["q_index"] = 0
            begin_question_locked()
            log("[게임] 시작!")
            return None
        if action == "next":
            if g["phase"] == "reveal":
                g["phase"] = "scoreboard"
                return None
            if g["phase"] == "scoreboard":
                if g["q_index"] + 1 < len(g["game_questions"]):
                    g["q_index"] += 1
                    begin_question_locked()
                else:
                    g["phase"] = "final"
                    log("[게임] 종료 — 최종 결과 발표")
                return None
            return "지금은 넘어갈 수 없어요."
        if action == "kick":
            if name in g["players"]:
                del g["players"][name]
                log(f"[강제퇴장] {name}")
            return None
        return "알 수 없는 동작이에요."


# ---------- 정보 아틀리에 (문제 실습 도구) ----------

ATELIER_PATH = os.path.join(BASE_DIR, "atelier.json")
ATELIER_LOCK = threading.Lock()
SESSIONS = {}  # token -> 만료 시각
SESSION_TTL = 48 * 3600
LAB_FORMATS = ("코딩", "객관식", "OX", "주관식")


class LabError(Exception):
    pass


class LabAuthError(LabError):
    pass


def _clip(s, n):
    return str(s or "")[:n]


def _norm(s):
    return "".join(str(s or "").split()).casefold()


def _pbkdf2(pw, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt_hex), 200_000).hex()


def default_lab_data():
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    problems = [
        {
            "id": 1, "title": "인사말 출력하기", "category": "파이썬 기초", "format": "코딩",
            "prompt": "print() 함수를 사용해 Hello, Python! 을 출력해 보세요.",
            "answer": 'print("Hello, Python!")',
            "tests": [{"input": "", "output": "Hello, Python!"}],
            "explanation": "print()는 괄호 안의 내용을 화면에 출력해요. 문자열은 따옴표로 감싸요.",
            "active": True, "created_at": now,
        },
        {
            "id": 2, "title": "두 변수의 합", "category": "변수와 자료형", "format": "코딩",
            "prompt": "변수 a에는 7, 변수 b에는 3을 저장하고, 두 수의 합을 출력해 보세요.",
            "answer": "a = 7\nb = 3\nprint(a + b)",
            "tests": [{"input": "", "output": "10"}],
            "explanation": "변수에 값을 저장하려면 = 를 사용하고, print(a + b)처럼 변수끼리 연산할 수 있어요.",
            "active": True, "created_at": now,
        },
        {
            "id": 3, "title": "조건이 참일 때만 실행하는 문법은?", "category": "조건문", "format": "주관식",
            "prompt": "파이썬에서 조건이 참(True)일 때만 코드를 실행하게 하는 문법의 이름을 적어 보세요.",
            "answer": "if",
            "explanation": "if 문은 조건이 참일 때 들여쓰기된 코드를 실행해요.",
            "active": True, "created_at": now,
        },
        {
            "id": 4, "title": "range(3)은 몇 번 반복할까?", "category": "반복문", "format": "OX",
            "prompt": "for i in range(3): 는 아래 코드를 총 3번 반복 실행한다.",
            "answer": "O",
            "explanation": "range(3)은 0, 1, 2를 만들어 총 3번 반복해요.",
            "active": True, "created_at": now,
        },
        {
            "id": 5, "title": "리스트를 만드는 기호는?", "category": "리스트와 딕셔너리", "format": "객관식",
            "prompt": "다음 중 파이썬에서 리스트를 만들 때 사용하는 기호는?",
            "options": ["( )", "[ ]", "{ }", "< >"],
            "answer": "1",
            "explanation": "리스트는 대괄호 [ ]로 만들고, 항목은 쉼표로 구분해요.",
            "active": True, "created_at": now,
        },
    ]
    materials = [
        {"id": 1, "category": "파이썬 기초", "title": "파이썬 공식 문서 (한국어)",
         "url": "https://docs.python.org/ko/3/", "note": "print, input 등 기본 함수 설명"},
    ]
    return {
        "pw_salt": "", "pw_hash": "",
        "categories": ["파이썬 기초", "변수와 자료형", "조건문", "반복문", "리스트와 딕셔너리"],
        "problems": problems, "materials": materials, "submissions": [],
    }


LAB = None


def load_lab():
    global LAB
    if os.path.isfile(ATELIER_PATH):
        try:
            with open(ATELIER_PATH, "r", encoding="utf-8") as f:
                LAB = json.load(f)
            if isinstance(LAB, dict) and "problems" in LAB:
                if _migrate_lab():
                    save_lab()
                return
        except Exception as e:
            print(f"[!] atelier.json 로드 실패: {e}")
    LAB = default_lab_data()
    save_lab()


def _migrate_lab():
    """예전 키워드 채점 구조를 테스트 케이스 구조로 변환"""
    changed = False
    for p in LAB.get("problems", []):
        if p.get("format") == "코딩":
            if not isinstance(p.get("tests"), list):
                t = {"input": p.get("expected_input", ""), "output": p.get("expected_output", "")}
                p["tests"] = [t] if (t["input"] or t["output"]) else []
                changed = True
            if p.pop("keywords", None) is not None:
                changed = True
    return changed


def save_lab():
    tmp = ATELIER_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(LAB, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ATELIER_PATH)


def new_session():
    t = secrets.token_hex(24)
    now = time.time()
    for k in [k for k, v in SESSIONS.items() if v < now]:
        SESSIONS.pop(k, None)
    SESSIONS[t] = now + SESSION_TTL
    return t


def session_token(headers):
    raw = headers.get("Cookie", "") or ""
    for part in raw.split(";"):
        part = part.strip()
        if part.startswith("lab_session="):
            t = part[len("lab_session="):]
            exp = SESSIONS.get(t)
            if exp and exp > time.time():
                return t
    return None


def lab_state_dict(headers):
    with ATELIER_LOCK:
        return {
            "password_set": bool(LAB.get("pw_hash")),
            "authed": session_token(headers) is not None,
            "categories": list(LAB.get("categories", [])),
            "materials": LAB.get("materials", []),
            "problem_count": len(LAB.get("problems", [])),
            "active_count": sum(1 for p in LAB.get("problems", []) if p.get("active")),
            "ai_ready": bool((LAB.get("ai") or {}).get("key")),
            "ai_model": (LAB.get("ai") or {}).get("model", "") or AI_DEFAULT_MODEL,
        }


def require_teacher(headers):
    if not session_token(headers):
        raise LabAuthError("교사 로그인이 필요해요")


# ---------- AI 해설 (Google Gemini) ----------

AI_DEFAULT_MODEL = "gemini-2.5-flash"


def ai_config():
    ai = LAB.get("ai") or {}
    return {"key": str(ai.get("key", "")).strip(),
            "model": str(ai.get("model", "")).strip() or AI_DEFAULT_MODEL}


def ai_explain(problem, submission_text, correct, case_results=None):
    cfg = ai_config()
    if not cfg["key"]:
        return None
    if problem["format"] == "코딩":
        tests = problem.get("tests") or []
        expected = "\n".join(f"- 입력: {t.get('input') or '(없음)'} / 기대 출력: {t.get('output') or '(없음)'}"
                             for t in tests) or "(테스트 없음)"
        if case_results:
            cases = "\n".join(
                f"- 입력: {c.get('input') or '(없음)'} / 기대 출력: {c.get('expected') or '(없음)'} / "
                f"학생 출력: {c.get('output') or '(없음)'} / {'일치' if c.get('match') else '불일치'}"
                + (f" / 힌트: {c.get('hint')}" if c.get("hint") else "")
                for c in case_results) or "(실행 결과 없음)"
        else:
            cases = "(실행 결과 없음)"
        detail = f"[테스트 케이스]\n{expected}\n\n[학생 코드 실행 결과]\n{cases}"
    else:
        detail = f"[정답] {problem.get('answer', '')}"
    prompt = f"""너는 한국 중학교 정보 수업의 다정하고 명확한 코딩 선생님이다. 아래 내용을 보고 학생용 해설을 한국어로 작성한다.

규칙:
- 3~5문장, 중학생 눈높이, 존댓말
- 이 문제가 다루는 핵심 개념(아래 [개념 카테고리])을 먼저 한두 문장으로 설명할 것 — 개념이 이 문제에서 왜 필요한지 포함
- 맞았을 경우: 그 개념이 학생의 제출에서 어떻게 사용되었는지 짚어 주고, 같은 개념을 활용할 수 있는 다음 상황을 한 줄 팁으로 제시
- 틀렸을 경우: 학생 출력(또는 제출)과 기대 출력을 한 글자 단위로 비교해 정확히 어느 부분이, 어떻게 다른지 먼저 짚어 준다. 예: 기대 출력이 'Hello, Python!'인데 학생이 'Hello!'를 출력했다면 → "Hello!라고 출력했는데, 문제는 'Hello, Python!'이 필요해요. 쉼표와 Python 부분이 빠졌어요."처럼 구체적으로. 이어서 개념 설명과 고치는 힌트(2단계 정도)를 알려 준다.
- 정답 코드, 완성된 코드, 완성된 문장(주관식 정답)을 절대 그대로 보여주지 말 것 — 비교 지적과 개념·힌트만
- 마크다운 기호(예: **, ##, 목록 기호) 없이 자연 문장으로만

[개념 카테고리] {problem['category']}
[문제 형식] {problem['format']}
[문제] {problem['prompt']}
{detail}
[채점 결과] {'정답' if correct else '오답'}
[학생 제출]
{str(submission_text)[:1200]}
{('[교사 참고 해설] ' + problem.get('explanation', '')) if problem.get('explanation') else ''}"""
    try:
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               + cfg["model"] + ":generateContent?key=" + cfg["key"])
        payload = {"contents": [{"parts": [{"text": prompt}]}],
                   "generationConfig": {"temperature": 0.4, "maxOutputTokens": 700}}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        text = str(((data.get("candidates") or [{}])[0].get("content") or {}).get("parts", [{}])[0].get("text", "")).strip()
        return text[:1500] if text else None
    except Exception as e:
        log(f"[아틀리에] AI 해설 생성 실패: {e}")
        return None


def student_explain(body):
    no = _clip(str(body.get("no", "")).strip(), 12)
    try:
        pid = int(body.get("problem_id"))
    except (TypeError, ValueError):
        raise LabError("문제를 찾을 수 없어요")
    with ATELIER_LOCK:
        prob = next((p for p in LAB.get("problems", []) if p.get("id") == pid), None)
        if not prob:
            raise LabError("문제를 찾을 수 없어요")
        sub = next((s for s in LAB.get("submissions", [])
                    if s.get("no") == no and s.get("problem_id") == pid), None)
    if not sub:
        return {"ok": True, "explanation": prob.get("explanation", "") or
                "먼저 문제를 제출하면 AI 해설이 표시돼요.", "source": "teacher"}
    ai_text = ai_explain(prob, sub.get("answer", ""), bool(sub.get("correct")), sub.get("case_results") or [])
    if ai_text:
        return {"ok": True, "explanation": ai_text, "source": "ai"}
    if prob.get("explanation"):
        return {"ok": True, "explanation": prob["explanation"], "source": "teacher"}
    return {"ok": True, "explanation": "", "source": "none"}


def validate_problem(data, categories):
    if not isinstance(data, dict):
        raise LabError("형식이 올바르지 않아요")
    title = _clip(str(data.get("title", "")).strip(), 60)
    if not title:
        raise LabError("문제 제목이 필요해요")
    category = _clip(str(data.get("category", "")).strip(), 20)
    if category not in categories:
        raise LabError("카테고리를 선택해 주세요")
    fmt = str(data.get("format", "")).strip()
    if fmt not in LAB_FORMATS:
        raise LabError("문제 형식이 올바르지 않아요")
    prompt = _clip(str(data.get("prompt", "")).strip(), 2000)
    if not prompt:
        raise LabError("문제 내용이 필요해요")
    p = {
        "title": title, "category": category, "format": fmt, "prompt": prompt,
        "explanation": _clip(str(data.get("explanation", "")).strip(), 1000),
        "active": bool(data.get("active", True)),
    }
    if fmt == "객관식":
        opts = [_clip(str(o).strip(), 100) for o in (data.get("options") or []) if str(o).strip()]
        if len(opts) < 2 or len(opts) > 6:
            raise LabError("객관식 보기는 2~6개 필요해요")
        try:
            ai = int(data.get("answer"))
        except (TypeError, ValueError):
            raise LabError("정답 보기를 선택해 주세요")
        if not 0 <= ai < len(opts):
            raise LabError("정답 보기 번호가 잘못됐어요")
        p["options"] = opts
        p["answer"] = str(ai)
    elif fmt == "OX":
        a = str(data.get("answer", "")).strip().upper()
        if a not in ("O", "X"):
            raise LabError("OX 정답은 O 또는 X여야 해요")
        p["answer"] = a
    elif fmt == "주관식":
        a = _clip(str(data.get("answer", "")).strip(), 200)
        if not a:
            raise LabError("주관식 정답이 필요해요")
        p["answer"] = a
    else:
        p["answer"] = _clip(str(data.get("answer", "")).strip("\n"), 3000)
        tests = []
        for t in (data.get("tests") or [])[:5]:
            if not isinstance(t, dict):
                continue
            tests.append({
                "input": _clip(str(t.get("input", "")).replace("\r", ""), 500),
                "output": _clip(str(t.get("output", "")).replace("\r", ""), 500),
            })
        p["tests"] = tests
        p.pop("keywords", None)
    return p


def _norm_out(s):
    lines = str(s or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


def _diff_hint(expected, got):
    """기대 출력과 학생 출력을 비교해 어디가 다른지 친절한 힌트를 만든다"""
    e = _norm_out(expected)
    g = _norm_out(got)
    if e == g:
        return ""
    if e.casefold() == g.casefold():
        for i in range(min(len(e), len(g))):
            if e[i] != g[i]:
                return f"{i + 1}번째 글자 '{g[i]}' → '{e[i]}' (대소문자가 달라요!)"
        return "대소문자가 달라요! 글자 크기를 확인해 보세요"
    if e.startswith(g):
        return f"출력 뒤에 '{e[len(g):]}' 부분이 더 필요해요"
    if g.startswith(e):
        return f"출력에서 '{g[len(e):]}' 부분은 없어야 해요"
    i = 0
    while i < min(len(e), len(g)) and e[i] == g[i]:
        i += 1
    if i < min(len(e), len(g)):
        return f"{i + 1}번째 글자가 달라요: '{g[i]}' → '{e[i]}'"
    return f"'{g}'가 아니라 '{e}'가 필요해요"


def grade_problem(p, answer, outputs=None):
    """returns (correct, case_results)"""
    fmt = p["format"]
    if fmt == "객관식":
        opts = p.get("options") or []
        correct_text = ""
        try:
            ci = int(p.get("answer", "-1"))
            correct_text = opts[ci] if 0 <= ci < len(opts) else ""
        except (TypeError, ValueError):
            pass
        try:
            ok = int(str(answer).strip()) == int(p.get("answer", "-1"))
        except (TypeError, ValueError):
            ok = False
        return ok, [], correct_text
    if fmt == "OX":
        ok = str(answer).strip().upper() == str(p.get("answer", "")).strip().upper()
        return ok, [], p.get("answer", "")
    if fmt == "주관식":
        answers = [a.strip() for a in str(p.get("answer", "")).split(",") if a.strip()]
        ok = _norm(answer) in [_norm(a) for a in answers]
        return ok, [], ", ".join(answers)
    # 코딩: 테스트 케이스 출력 일치 방식 (브라우저 Pyodide 실행 결과 비교)
    tests = p.get("tests") or []
    outs = [str(o or "") for o in (outputs or [])]
    if not tests:
        return bool(str(answer).strip()), [], ""
    case_results = []
    all_ok = len(outs) >= len(tests)
    for i, t in enumerate(tests):
        got = outs[i] if i < len(outs) else ""
        match = _norm_out(got) == _norm_out(t.get("output", ""))
        case_results.append({"input": t.get("input", ""), "output": got,
                             "expected": t.get("output", ""), "match": match,
                             "hint": "" if match else _diff_hint(t.get("output", ""), got)})
        all_ok = all_ok and match
    return all_ok, case_results, ""


def student_problem(p):
    return {
        "id": p["id"], "title": p["title"], "category": p["category"],
        "format": p["format"], "prompt": p["prompt"],
        "options": p.get("options") or [],
        "tests": p.get("tests") or [],
    }


def student_submit(body):
    no = _clip(str(body.get("no", "")).strip(), 12)
    name = _clip(str(body.get("name", "")).strip(), 12)
    if not no or not name:
        raise LabError("이름과 번호를 입력한 뒤 시작해 주세요")
    try:
        pid = int(body.get("problem_id"))
    except (TypeError, ValueError):
        raise LabError("문제를 찾을 수 없어요")
    answer = "" if body.get("answer") is None else str(body.get("answer"))
    if len(answer) > 5000:
        answer = answer[:5000]
    outputs = body.get("outputs") if isinstance(body.get("outputs"), list) else []
    outputs = [str(o if o is not None else "")[:2000] for o in outputs[:5]]
    with ATELIER_LOCK:
        prob = next((p for p in LAB.get("problems", []) if p.get("id") == pid), None)
        if not prob or not prob.get("active"):
            raise LabError("지금 풀 수 없는 문제예요")
        ok, case_results, correct_text = grade_problem(prob, answer, outputs)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        for s in LAB.get("submissions", []):
            if s.get("no") == no and s.get("problem_id") == pid:
                s.update({"name": name, "answer": answer[:500], "correct": ok, "case_results": case_results, "submitted_at": now})
                break
        else:
            LAB.setdefault("submissions", []).append(
                {"no": no, "name": name, "problem_id": pid, "answer": answer[:500],
                 "correct": ok, "case_results": case_results, "submitted_at": now})
        save_lab()
        resp = {"ok": True, "correct": ok, "case_results": case_results,
                "explanation": prob.get("explanation", "")}
        return resp


def student_analysis(query):
    no = _clip(str((query.get("no") or [""])[0]).strip(), 12)
    name = _clip(str((query.get("name") or [""])[0]).strip(), 12)
    if not no:
        raise LabError("학생 정보가 없어요. 이름·번호를 입력하고 시작해 주세요")
    with ATELIER_LOCK:
        probs = [p for p in LAB.get("problems", []) if p.get("active")]
        subs = [s for s in LAB.get("submissions", []) if s.get("no") == no]
        solved_ids = {s["problem_id"] for s in subs if s.get("correct")}
        tried_ids = {s["problem_id"] for s in subs}
        cats = {}
        for p in probs:
            cats.setdefault(p["category"], []).append(p)
        cat_rows = []
        for cname in LAB.get("categories", []):
            plist = cats.get(cname)
            if not plist:
                continue
            solved = sum(1 for p in plist if p["id"] in solved_ids)
            cat_rows.append({"name": cname, "total": len(plist), "solved": solved,
                             "percent": (solved / len(plist) * 100) if plist else 0})
        overall_solved = sum(1 for p in probs if p["id"] in solved_ids)
        return {
            "identity": {"name": name or no, "no": no},
            "overall": {"total": len(probs), "solved": overall_solved,
                        "percent": (overall_solved / len(probs) * 100) if probs else 0,
                        "submissions": len(subs)},
            "categories": cat_rows,
            "problems": [{"id": p["id"], "title": p["title"], "category": p["category"],
                          "format": p["format"],
                          "status": "solved" if p["id"] in solved_ids else ("tried" if p["id"] in tried_ids else "none")}
                         for p in probs],
            "materials": LAB.get("materials", []),
        }


def teacher_analysis():
    with ATELIER_LOCK:
        probs_all = LAB.get("problems", [])
        probs = [p for p in probs_all if p.get("active")]
        subs = LAB.get("submissions", [])
        students = {}
        for s in subs:
            st = students.setdefault(s["no"], {"no": s["no"], "name": s.get("name") or s["no"],
                                               "attempted": 0, "solved": 0, "last_at": ""})
            st["attempted"] += 1
            if s.get("correct"):
                st["solved"] += 1
            if s.get("submitted_at", "") > st["last_at"]:
                st["last_at"] = s["submitted_at"]
        rows = []
        for st in students.values():
            st["percent"] = (st["solved"] / len(probs) * 100) if probs else 0
            rows.append(st)
        rows.sort(key=lambda x: (-x["percent"], x["name"]))
        correct_by_student = {}
        for s in subs:
            if s.get("correct"):
                correct_by_student.setdefault(s["no"], set()).add(s["problem_id"])
        cats = {}
        for p in probs:
            cats.setdefault(p["category"], []).append(p)
        cat_rows = []
        for cname in LAB.get("categories", []):
            plist = cats.get(cname)
            if not plist:
                continue
            pids = {p["id"] for p in plist}
            pcts = []
            for st in rows:
                solved = len(correct_by_student.get(st["no"], set()) & pids)
                pcts.append(solved / len(plist) * 100)
            cat_rows.append({"name": cname, "total": len(plist),
                             "avg_percent": (sum(pcts) / len(pcts)) if pcts else 0})
        prob_rows = []
        for p in sorted(probs_all, key=lambda x: x["id"]):
            psubs = [s for s in subs if s.get("problem_id") == p["id"]]
            attempted = len({s["no"] for s in psubs})
            solved = len({s["no"] for s in psubs if s.get("correct")})
            prob_rows.append({"id": p["id"], "title": p["title"], "category": p["category"],
                              "format": p["format"], "active": p.get("active", True),
                              "attempted": attempted, "solved": solved,
                              "rate": (solved / attempted * 100) if attempted else 0})
        return {
            "students": rows,
            "categories": cat_rows,
            "problems": prob_rows,
            "totals": {"active_problems": len(probs), "submissions": len(subs)},
        }


def lab_api(handler, method, path, query, body):
    """정보 아틀리에 API·페이지 처리. 처리했으면 True."""
    h = handler
    try:
        # ---- 페이지 ----
        if method == "GET" and (path == "/lab" or path == "/lab/" or path == "/lab/index.html"):
            h.send_atelier_page("index.html")
            return True
        if method == "GET" and path.startswith("/lab/"):
            h.send_atelier_page(path[len("/lab/"):])
            return True

        # ---- 공개 ----
        if method == "GET" and path == "/api/lab/state":
            h.send_json(lab_state_dict(h.headers))
            return True
        if method == "GET" and path == "/api/lab/materials":
            with ATELIER_LOCK:
                h.send_json({"materials": LAB.get("materials", [])})
            return True
        if method == "GET" and path == "/api/lab/student/problems":
            with ATELIER_LOCK:
                probs = [student_problem(p) for p in LAB.get("problems", []) if p.get("active")]
            h.send_json({"problems": probs})
            return True
        if method == "GET" and path == "/api/lab/student/analysis":
            h.send_json(student_analysis(query))
            return True
        if method == "POST" and path == "/api/lab/student/submit":
            h.send_json(student_submit(body))
            return True
        if method == "POST" and path == "/api/lab/student/explain":
            h.send_json(student_explain(body))
            return True

        # ---- 인증 ----
        if method == "POST" and path == "/api/lab/auth/setup":
            with ATELIER_LOCK:
                if LAB.get("pw_hash"):
                    raise LabError("이미 비밀번호가 설정되어 있어요. 로그인해 주세요")
                pw = str(body.get("password", ""))
                if len(pw) < 4:
                    raise LabError("비밀번호는 4자 이상으로 정해 주세요")
                LAB["pw_salt"] = secrets.token_hex(16)
                LAB["pw_hash"] = _pbkdf2(pw, LAB["pw_salt"])
                save_lab()
            token = new_session()
            h.send_json({"ok": True}, extra_headers=[
                ("Set-Cookie", f"lab_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}")])
            return True
        if method == "POST" and path == "/api/lab/auth/login":
            with ATELIER_LOCK:
                pw = str(body.get("password", ""))
                ok = bool(LAB.get("pw_hash")) and secrets.compare_digest(_pbkdf2(pw, LAB["pw_salt"]), LAB["pw_hash"])
            if not ok:
                h.send_json({"error": "비밀번호가 올바르지 않아요"}, 401)
                return True
            token = new_session()
            h.send_json({"ok": True}, extra_headers=[
                ("Set-Cookie", f"lab_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}")])
            return True
        if method == "POST" and path == "/api/lab/auth/logout":
            t = session_token(h.headers)
            if t:
                SESSIONS.pop(t, None)
            h.send_json({"ok": True}, extra_headers=[
                ("Set-Cookie", "lab_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")])
            return True

        # ---- 교사 전용 ----
        if path.startswith("/api/lab/teacher") or path.startswith("/api/lab/problems") \
                or path.startswith("/api/lab/categories") or path.startswith("/api/lab/ai-config"):
            require_teacher(h.headers)

        if method == "GET" and path == "/api/lab/problems":
            with ATELIER_LOCK:
                probs = sorted(LAB.get("problems", []), key=lambda x: -x.get("id", 0))
            h.send_json({"problems": probs})
            return True
        if method == "POST" and path == "/api/lab/problems":
            with ATELIER_LOCK:
                p = validate_problem(body, LAB.get("categories", []))
                p["id"] = max([x.get("id", 0) for x in LAB.get("problems", [])] or [0]) + 1
                p["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                LAB.setdefault("problems", []).append(p)
                save_lab()
            log(f"[아틀리에] 문제 추가: {p['title']}")
            h.send_json({"problem": p})
            return True
        if method == "PUT" and path.startswith("/api/lab/problems/"):
            try:
                pid = int(path.rsplit("/", 1)[1])
            except ValueError:
                raise LabError("문제를 찾을 수 없어요")
            with ATELIER_LOCK:
                prob = next((p for p in LAB.get("problems", []) if p.get("id") == pid), None)
                if not prob:
                    raise LabError("문제를 찾을 수 없어요")
                merged = {**prob, **(body if isinstance(body, dict) else {})}
                p = validate_problem(merged, LAB.get("categories", []))
                p["id"] = pid
                p["created_at"] = prob.get("created_at", "")
                LAB["problems"][LAB["problems"].index(prob)] = p
                save_lab()
            h.send_json({"problem": p})
            return True
        if method == "DELETE" and path.startswith("/api/lab/problems/"):
            try:
                pid = int(path.rsplit("/", 1)[1])
            except ValueError:
                raise LabError("문제를 찾을 수 없어요")
            with ATELIER_LOCK:
                LAB["problems"] = [p for p in LAB.get("problems", []) if p.get("id") != pid]
                LAB["submissions"] = [s for s in LAB.get("submissions", []) if s.get("problem_id") != pid]
                save_lab()
            log(f"[아틀리에] 문제 삭제: id={pid}")
            h.send_json({"ok": True})
            return True
        if method == "POST" and path == "/api/lab/categories":
            name = _clip(str(body.get("name", "")).strip(), 20)
            if not name:
                raise LabError("카테고리 이름을 입력해 주세요")
            with ATELIER_LOCK:
                if name in LAB.get("categories", []):
                    raise LabError("이미 있는 카테고리예요")
                LAB.setdefault("categories", []).append(name)
                save_lab()
            h.send_json({"ok": True, "categories": LAB.get("categories", [])})
            return True
        if method == "DELETE" and path == "/api/lab/categories":
            name = _clip(str((query.get("name") or [""])[0]).strip(), 20)
            with ATELIER_LOCK:
                used = any(p.get("category") == name for p in LAB.get("problems", []))
                if used:
                    raise LabError("문제가 있는 카테고리는 삭제할 수 없어요")
                if name not in LAB.get("categories", []):
                    raise LabError("카테고리를 찾을 수 없어요")
                LAB["categories"].remove(name)
                save_lab()
            h.send_json({"ok": True})
            return True
        if method == "GET" and path == "/api/lab/teacher/analysis":
            h.send_json(teacher_analysis())
            return True
        if method == "GET" and path == "/api/lab/ai-config":
            with ATELIER_LOCK:
                cfg = LAB.get("ai") or {}
            h.send_json({"key_set": bool(str(cfg.get("key", "")).strip()),
                         "model": str(cfg.get("model", "")).strip() or AI_DEFAULT_MODEL})
            return True
        if method == "PUT" and path == "/api/lab/ai-config":
            key = _clip(str(body.get("key", "")).strip(), 300)
            model = _clip(str(body.get("model", "")).strip(), 60) or AI_DEFAULT_MODEL
            with ATELIER_LOCK:
                LAB["ai"] = {"key": key, "model": model}
                save_lab()
            log("[아틀리에] AI 설정 저장")
            h.send_json({"ok": True, "ai_ready": bool(key)})
            return True

        # ---- 학습자료 ----
        if method == "POST" and path == "/api/lab/materials":
            require_teacher(h.headers)
            m = {
                "category": _clip(str(body.get("category", "")).strip(), 20),
                "title": _clip(str(body.get("title", "")).strip(), 80),
                "url": _clip(str(body.get("url", "")).strip(), 300),
                "note": _clip(str(body.get("note", "")).strip(), 150),
            }
            if not m["title"] or not m["url"].lower().startswith(("http://", "https://")):
                raise LabError("제목과 http(s) URL이 필요해요")
            with ATELIER_LOCK:
                if m["category"] not in LAB.get("categories", []):
                    raise LabError("카테고리를 먼저 만들어 주세요")
                m["id"] = max([x.get("id", 0) for x in LAB.get("materials", [])] or [0]) + 1
                LAB.setdefault("materials", []).append(m)
                save_lab()
            h.send_json({"material": m})
            return True
        if method == "DELETE" and path.startswith("/api/lab/materials/"):
            require_teacher(h.headers)
            try:
                mid = int(path.rsplit("/", 1)[1])
            except ValueError:
                raise LabError("자료를 찾을 수 없어요")
            with ATELIER_LOCK:
                LAB["materials"] = [m for m in LAB.get("materials", []) if m.get("id") != mid]
                save_lab()
            h.send_json({"ok": True})
            return True

        return False
    except LabAuthError as e:
        h.send_json({"error": str(e)}, 401)
        return True
    except LabError as e:
        h.send_json({"error": str(e)}, 400)
        return True
    except Exception:
        traceback.print_exc()
        h.send_json({"error": "server error"}, 500)
        return True


# ---------- HTTP ----------

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
        ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml",
        ".ico": "image/x-icon", ".wasm": "application/wasm",
        ".zip": "application/zip"}


class Handler(BaseHTTPRequestHandler):
    server_version = "QuizLive/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def send_data(self, data, ctype, status=200, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, obj, status=200, extra_headers=None):
        self.send_data(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", status, extra_headers)

    def send_page(self, fname):
        try:
            with open(os.path.join(STATIC_DIR, fname), "rb") as f:
                data = f.read()
            ext = os.path.splitext(fname)[1].lower()
            self.send_data(data, MIME.get(ext, "application/octet-stream"))
        except OSError:
            self.send_json({"error": "not found"}, 404)

    def send_atelier_page(self, rel):
        """Code Atelier(정보 아틀리에) 프론트 정적 파일 서빙 + SPA 폴백"""
        base = os.path.abspath(os.path.join(STATIC_DIR, "atelier"))
        rel = (rel or "index.html").lstrip("/")
        full = os.path.abspath(os.path.join(base, rel))
        if not full.startswith(base + os.sep):
            self.send_json({"error": "not found"}, 404)
            return
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            if "." not in os.path.basename(rel):
                full = os.path.join(base, "index.html")
            else:
                self.send_json({"error": "not found"}, 404)
                return
        try:
            with open(full, "rb") as f:
                data = f.read()
            ext = os.path.splitext(full)[1].lower()
            self.send_data(data, MIME.get(ext, "application/octet-stream"))
        except OSError:
            self.send_json({"error": "not found"}, 404)

    def read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        if n <= 0 or n > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        try:
            parts = urlsplit(self.path)
            path = parts.path
            query = parse_qs(parts.query)
            if path.startswith("/lab") or path.startswith("/api/lab/"):
                if lab_api(self, "GET", path, query, {}):
                    return
            if path in ("/", "/index.html"):
                self.send_page("index.html")
            elif path == "/host":
                self.send_page("host.html")
            elif path == "/play":
                self.send_page("play.html")
            elif path in ("/python", "/python/"):
                self.send_page("python.html")
            elif path.startswith("/static/"):
                name = os.path.normpath(path[len("/static/"):])
                full = os.path.abspath(os.path.join(STATIC_DIR, name))
                if full.startswith(os.path.abspath(STATIC_DIR) + os.sep) and os.path.isfile(full):
                    with open(full, "rb") as f:
                        data = f.read()
                    ext = os.path.splitext(full)[1].lower()
                    self.send_data(data, MIME.get(ext, "application/octet-stream"))
                else:
                    self.send_json({"error": "not found"}, 404)
            elif path == "/api/info":
                with GAME_LOCK:
                    tick_locked()
                    info = {"pin": GAME["pin"] or None, "phase": GAME["phase"],
                            "players": len(GAME["players"]), "title": QUIZ["title"],
                            "q_total": len(QUIZ["questions"]),
                            "lan_ip": get_lan_ip(), "port": PORT}
                self.send_json(info)
            elif path == "/api/host":
                self.send_json(host_state())
            elif path == "/api/play":
                name = (parse_qs(parts.query).get("name") or [""])[0]
                self.send_json(play_state(name))
            elif path == "/api/questions":
                self.send_json(QUIZ)
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self.send_json({"error": "server error"}, 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            parts = urlsplit(self.path)
            path = parts.path
            body = self.read_json()
            if path.startswith("/lab") or path.startswith("/api/lab/"):
                if lab_api(self, "POST", path, parse_qs(parts.query), body):
                    return
            if path == "/api/join":
                err = join(body.get("pin", ""), body.get("name", ""))
                self.send_json({"ok": err is None, "error": err or ""})
            elif path == "/api/answer":
                err = submit_answer(body.get("name", ""), body.get("choice"))
                self.send_json({"ok": err is None, "error": err or ""})
            elif path == "/api/host/action":
                err = host_action(body.get("action", ""), body.get("name", ""))
                self.send_json({"ok": err is None, "error": err or ""})
            elif path == "/api/questions":
                try:
                    count = save_questions(body)
                    self.send_json({"ok": True, "count": count})
                except Exception as e:
                    self.send_json({"ok": False, "error": str(e)})
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self.send_json({"error": "server error"}, 500)
            except Exception:
                pass

    def do_PUT(self):
        try:
            parts = urlsplit(self.path)
            path = parts.path
            body = self.read_json()
            if path.startswith("/lab") or path.startswith("/api/lab/"):
                if lab_api(self, "PUT", path, parse_qs(parts.query), body):
                    return
            self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self.send_json({"error": "server error"}, 500)
            except Exception:
                pass

    def do_DELETE(self):
        try:
            parts = urlsplit(self.path)
            path = parts.path
            if path.startswith("/lab") or path.startswith("/api/lab/"):
                if lab_api(self, "DELETE", path, parse_qs(parts.query), {}):
                    return
            self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self.send_json({"error": "server error"}, 500)
            except Exception:
                pass


class QuizServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # 폰 화면 절전 등으로 클라이언트가 연결을 끊는 것은 정상 상황이므로 조용히 무시
        if sys.exc_info()[0] in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError):
            return
        traceback.print_exc()


def main():
    global PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--no-browser", action="store_true",
                    default=bool(os.environ.get("PORT")))
    args = ap.parse_args()
    PORT = args.port
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    load_questions()
    load_lab()
    try:
        httpd = QuizServer(("0.0.0.0", PORT), Handler)
    except OSError:
        print(f"[!] {PORT}번 포트가 이미 사용 중입니다. 다른 서버가 실행 중인지 확인해 주세요.")
        sys.exit(2)
    print("=" * 56)
    print("  정보 아틀리에 서버 실행 중")
    print("-" * 56)
    print(f"  교사 화면 : http://localhost:{PORT}/lab/#/teacher")
    print(f"  학생 접속 : http://{get_lan_ip()}:{PORT}/lab/#/student")
    print("  (학생은 교사와 같은 Wi-Fi/네트워크에 연결)")
    print("  * 클라우드 버전: https://chavi55.onrender.com/lab")
    print("  * 종료: Ctrl+C (또는 창 닫기)")
    print("=" * 56, flush=True)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{PORT}/lab/#/teacher")).start()
    while True:
        try:
            httpd.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            print("서버를 종료합니다.")
            sys.exit(0)
        except Exception:
            traceback.print_exc()
            time.sleep(1)


if __name__ == "__main__":
    main()
