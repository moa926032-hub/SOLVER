import importlib
import json
import subprocess
import sys
import time
import argparse

def ensure_packages(packages):
    for pkg in packages:
        try:
            importlib.import_module(pkg)
        except ImportError:
            cmd = [sys.executable, "-m", "pip", "install", "-q", pkg]
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if res.returncode != 0:
                subprocess.check_call(
                    cmd + ["--break-system-packages"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

ensure_packages(["requests", "rich"])

import requests
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

TIMEOUT = 20
console = Console()

def safe_json(response):
    try:
        return response.json()
    except Exception:
        return None

def make_progress():
    return Progress(
        SpinnerColumn("dots", style="bold cyan"),
        TextColumn("[bold white]{task.description}"),
        BarColumn(bar_width=24, style="grey37", complete_style="bold green", finished_style="bold bright_green"),
        TaskProgressColumn(style="bold yellow"),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TextColumn("[dim cyan]{task.fields[status]}"),
        console=console,
        transient=False,
    )

class QureoClient:
    BASE = "https://me-tp.qureo.education/api/study"
    LOGIN = "https://me-portal.qureo.education/oauth2/password-login"
    USERINFO = "https://me-portal.qureo.education/oauth2/userinfo"
    TP_HOME = "https://me-tp.qureo.education/"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
            "Content-Type": "application/json",
            "origin": "https://me-portal.qureo.education",
            "referer": "https://me-portal.qureo.education/login",
            "accept-language": "en-US,en;q=0.9,ar;q=0.8",
        })
        self.userinfo = {}
        self.courses = []

    def login(self, student_id, password):
        r = self.session.post(
            self.LOGIN,
            data=json.dumps({"student_id": student_id, "password": password}),
            allow_redirects=False,
            timeout=TIMEOUT,
        )
        if r.status_code not in (200, 302):
            raise RuntimeError(f"Login failed (HTTP {r.status_code})")
        u = self.session.get(self.USERINFO, timeout=TIMEOUT)
        self.userinfo = safe_json(u) or {}
        if not self.userinfo.get("id"):
            raise RuntimeError("Invalid credentials or missing profile")
        raw = self.userinfo.get("available_courses") or []
        real = [
            c for c in raw
            if "trial" not in (c.get("course_code") or "").lower()
            and "trial" not in (c.get("content_id") or "").lower()
        ]
        self.courses = real or raw
        return self.userinfo

    def is_grade_two(self):
        has_python = any(
            "python" in ((c.get("course_code") or c.get("content_id") or "").lower())
            for c in self.courses
        )
        return len(self.courses) >= 2 or has_python

    def find_course(self, keyword):
        kw = keyword.lower()
        for c in self.courses:
            text = f"{c.get('course_code', '')} {c.get('content_id', '')}".lower()
            if kw == "javascript" and ("javascript" in text or ("java" in text and "python" not in text)):
                return c
            if kw == "python" and "python" in text:
                return c
        return None

    def _select_course(self, content_id):
        self.session.headers.update({"referer": self.TP_HOME})
        self.session.cookies.set("student_selected_content_id", content_id, domain="me-tp.qureo.education")
        self.session.get(f"{self.TP_HOME}?content_id={content_id}", allow_redirects=False, timeout=TIMEOUT)
        self.session.get(f"{self.BASE}/students/me", allow_redirects=True, timeout=TIMEOUT)

    def _get_section_id(self, content_id):
        data = safe_json(self.session.get(f"{self.BASE}/courses/{content_id}", timeout=TIMEOUT)) or {}
        basic = data.get("basic_section")
        if isinstance(basic, dict) and basic.get("id") is not None:
            return basic["id"]
        sections = data.get("sections")
        if isinstance(sections, list) and sections:
            if isinstance(sections[0], dict) and sections[0].get("id") is not None:
                return sections[0]["id"]
        return 5 if content_id == "javascript_ar" else None

    def _get_test_result(self, cid, is_review):
        path = "review/result" if is_review else "test/result"
        data = safe_json(self.session.get(f"{self.BASE}/students/chapters/{cid}/{path}", timeout=TIMEOUT))
        if isinstance(data, dict) and "result" in data:
            return data["result"]
        return data or {}

    def _extract_answers(self, result):
        answers = {}
        for c in result.get("choice_results", []):
            correct = next((ch["choice"]["id"] for ch in c.get("choices", []) if ch.get("correct")), None)
            if correct:
                answers[c["question_id"]] = ("choice", correct)
        for d in result.get("description_results", []):
            if d.get("model_answer"):
                answers[d["question_id"]] = ("desc", d["model_answer"])
        return answers

    def _solve_try_talk(self, talk, default_lang):
        talk_type = talk.get("type", "try")
        body = talk.get("body") or {}
        codes = body.get("codes", [])
        if not codes:
            return
        code = next((c for c in codes if c.get("editable")), codes[0])
        editor_type = body.get("editor_type", "code")
        lang = code.get("lang") or default_lang
        url = f"{self.BASE}/students/lecture-talks/{talk['id']}"

        if editor_type == "demo":
            self.session.put(
                url,
                json={"value": {f"{talk_type}_demo": {"lang": lang, "answer": code.get("code") or ""}}},
                timeout=TIMEOUT,
            )
            return

        if editor_type in ("part", "line"):
            placeholders = code.get("placeholders") or []
            if not placeholders:
                return
            partial = [{"name": p.get("name", ""), "code": p.get("model_answer", "")} for p in placeholders]
            r = self.session.put(
                url,
                json={"value": {f"{talk_type}_partial": {"lang": lang, "answers": partial}}},
                timeout=TIMEOUT,
            )
            if r.status_code != 200:
                values = [p.get("model_answer", "") for p in placeholders]
                self.session.put(
                    url,
                    json={"value": {talk_type: {"lang": lang, "answer": json.dumps({"parts": values})}}},
                    timeout=TIMEOUT,
                )
            return

        answer = code.get("model_answer") or ""
        if answer:
            self.session.put(url, json={"value": {talk_type: {"lang": lang, "answer": answer}}}, timeout=TIMEOUT)

    def _solve_talk(self, talk, default_lang):
        ttype = talk.get("type")
        body = talk.get("body") or {}
        url = f"{self.BASE}/students/lecture-talks/{talk['id']}"

        if ttype in ("choice_question", "choice_survey"):
            choices = body.get("choices", [])
            if choices:
                self.session.put(url, json={"value": {ttype: {"value": choices[0]["value"]}}}, timeout=TIMEOUT)
        elif ttype == "puzzle":
            model = body.get("model_answer") or [
                c.get("id", "") for c in (body.get("cards") or []) if isinstance(c, dict) and c.get("id")
            ]
            if model:
                self.session.put(url, json={"value": {"puzzle": {"answer": model}}}, timeout=TIMEOUT)
        elif ttype in ("try", "try_large"):
            self._solve_try_talk(talk, default_lang)

    def _solve_lectures(self, lectures, default_lang):
        for lec in lectures:
            lid = lec["id"]
            self.session.put(f"{self.BASE}/students/lectures/{lid}", timeout=TIMEOUT)
            lec_data = safe_json(self.session.get(f"{self.BASE}/lectures/{lid}", timeout=TIMEOUT)) or {}
            for talk in lec_data.get("talks", []):
                self._solve_talk(talk, default_lang)
            self.session.put(f"{self.BASE}/students/lectures/{lid}/complete", timeout=TIMEOUT)
            time.sleep(0.05)

    def _answer_question(self, qid, typ, val, is_review):
        path = "review-questions" if is_review else "test-questions"
        if typ == "choice":
            self.session.put(f"{self.BASE}/students/{path}/{qid}/answer/choice", json={"choice_id": val}, timeout=TIMEOUT)
        else:
            self.session.put(f"{self.BASE}/students/{path}/{qid}/answer/description", json={"answer_code": val}, timeout=TIMEOUT)

    def _fill_dummy(self, questions, is_review, default_lang):
        for q in questions:
            if not isinstance(q, dict) or "question" not in q:
                continue
            qid = q["question"]["id"]
            choices = q.get("choices")
            if choices:
                self._answer_question(qid, "choice", choices[0]["id"], is_review)
            elif q.get("descriptions") is not None:
                descs = q.get("descriptions") or []
                editable = next((d for d in descs if isinstance(d, dict) and d.get("editable")), descs[0] if descs else {})
                lang = (editable.get("lang") if isinstance(editable, dict) else None) or default_lang
                dummy = "print()" if "python" in lang.lower() else "console.log();"
                self._answer_question(qid, "desc", dummy, is_review)

    def _submit_answers(self, questions, answers, is_review):
        for q in questions:
            if not isinstance(q, dict) or "question" not in q:
                continue
            qid = q["question"]["id"]
            if qid in answers:
                typ, val = answers[qid]
                self._answer_question(qid, typ, val, is_review)

    def _solve_chapter(self, cid, is_review, default_lang, update_status):
        ch = safe_json(self.session.get(f"{self.BASE}/chapters/{cid}", timeout=TIMEOUT)) or {}
        if not ch:
            return
        lectures = sorted(ch.get("lectures", []), key=lambda x: x["seq"])
        total = (ch.get("chapter") or {}).get("question_count", 0)
        state = safe_json(self.session.get(f"{self.BASE}/students/chapters/{cid}/test", timeout=TIMEOUT)) or {}
        result = self._get_test_result(cid, is_review)
        correct = result.get("correct_count", 0) or 0

        if not state.get("work_in_process") and state.get("visible_result") and correct >= total:
            return

        update_status(f"Ch {cid} | Lectures")
        self._solve_lectures(lectures, default_lang)

        if total == 0 or correct >= total:
            return

        update_status(f"Ch {cid} | Test")
        self.session.put(f"{self.BASE}/students/chapters/{cid}/test", timeout=TIMEOUT)
        time.sleep(0.3)

        questions = safe_json(self.session.get(f"{self.BASE}/chapters/{cid}/test", timeout=TIMEOUT))
        if not isinstance(questions, list) or not questions:
            return

        answers = self._extract_answers(result)
        if len(answers) < len(questions):
            update_status(f"Ch {cid} | Probing")
            self._fill_dummy(questions, is_review, default_lang)
            time.sleep(1.2)
            result = self._get_test_result(cid, is_review)
            correct = result.get("correct_count", 0) or 0
            answers = self._extract_answers(result)
            if correct >= total:
                return
            self.session.put(f"{self.BASE}/students/chapters/{cid}/test", timeout=TIMEOUT)
            time.sleep(0.5)
            questions = safe_json(self.session.get(f"{self.BASE}/chapters/{cid}/test", timeout=TIMEOUT))
            if not isinstance(questions, list) or not questions:
                return

        update_status(f"Ch {cid} | Solving")
        self._submit_answers(questions, answers, is_review)
        time.sleep(1.2)

        result = self._get_test_result(cid, is_review)
        correct = result.get("correct_count", 0) or 0
        if 0 < correct < total:
            answers = self._extract_answers(result)
            if len(answers) >= total:
                update_status(f"Ch {cid} | Retry")
                self.session.put(f"{self.BASE}/students/chapters/{cid}/test", timeout=TIMEOUT)
                time.sleep(0.5)
                questions = safe_json(self.session.get(f"{self.BASE}/chapters/{cid}/test", timeout=TIMEOUT))
                if isinstance(questions, list) and questions:
                    self._submit_answers(questions, answers, is_review)
                    time.sleep(1.2)

    def run_course(self, course, label):
        content_id = course["content_id"]
        default_lang = "python" if "python" in content_id.lower() else "javascript"
        self._select_course(content_id)
        section_id = self._get_section_id(content_id)
        if section_id is None:
            console.print(f"[bold red]Section not found for {label}[/]")
            return

        chapters = (safe_json(self.session.get(f"{self.BASE}/sections/{section_id}", timeout=TIMEOUT)) or {}).get("chapters", [])
        if not chapters:
            console.print(f"[bold red]No chapters found for {label}[/]")
            return

        chapters.sort(key=lambda c: c.get("seq", 0))
        with make_progress() as progress:
            task = progress.add_task(label, total=len(chapters), status="Starting")
            for ch in chapters:
                is_review = ch.get("test_type", "review") == "review"
                try:
                    self._solve_chapter(
                        ch["id"],
                        is_review,
                        default_lang,
                        lambda st: progress.update(task, status=st),
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    progress.update(task, status=f"Err: {str(exc)[:24]}")
                progress.advance(task)
                time.sleep(0.1)
            progress.update(task, status="Done")


class SPXClient:
    BASE = "https://egy.app.spx-learning-square.com/api/v1"
    ORIGIN = "https://egy.app.spx-learning-square.com"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:159.0) Gecko/20100101 Firefox/159.0",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": self.ORIGIN,
            "Referer": f"{self.ORIGIN}/login",
            "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        })
        self.subjects = []

    def login(self, login_id, password):
        r = self.session.post(
            f"{self.BASE}/auth/login",
            json={"login_id": login_id, "password": password},
            timeout=TIMEOUT,
        )
        body = safe_json(r) or {}
        if r.status_code != 200 or not body.get("success"):
            raise RuntimeError(f"SPX login failed (HTTP {r.status_code})")
        me = safe_json(self.session.get(f"{self.BASE}/auth/me", timeout=TIMEOUT)) or {}
        userinfo = me.get("data") or body.get("data") or {}
        if not userinfo.get("id"):
            raise RuntimeError("SPX profile unavailable")
        raw = (safe_json(self.session.get(f"{self.BASE}/subjects", timeout=TIMEOUT)) or {}).get("data") or []
        valid = [s for s in raw if s.get("kind") != "game" and (s.get("total_items") or 0) > 0]
        self.subjects = valid or [s for s in raw if s.get("kind") != "game"]
        return userinfo

    def _get_curriculum(self, subject_id):
        data = (safe_json(self.session.get(f"{self.BASE}/subjects/{subject_id}/curriculum", timeout=TIMEOUT)) or {}).get("data")
        return data if isinstance(data, list) else []

    def _solve_chapter(self, subject_id, chapter_id):
        r = self.session.get(f"{self.BASE}/chapters/{chapter_id}/blocks", timeout=TIMEOUT)
        if r.status_code != 200:
            return False
        data = (safe_json(r) or {}).get("data") or {}
        chapter_info = data.get("chapter") or {}
        if chapter_info.get("is_completed"):
            return True

        cleared = set(chapter_info.get("cleared_block_ids") or [])
        q_blocks = [b for b in (data.get("blocks") or []) if b.get("type") == "question" and b.get("id") not in cleared]

        for block in q_blocks:
            for opt in (block.get("options") or []):
                ans_r = self.session.post(
                    f"{self.BASE}/chapters/{chapter_id}/answer",
                    json={"block_id": block["id"], "selected_option_id": opt["id"], "hint_used": 0},
                    timeout=TIMEOUT,
                )
                ans_data = (safe_json(ans_r) or {}).get("data") or {}
                if ans_r.status_code == 200 and ans_data.get("is_correct"):
                    break
                time.sleep(0.03)

        comp_r = self.session.post(f"{self.BASE}/chapters/{chapter_id}/complete", json={}, timeout=TIMEOUT)
        comp_body = safe_json(comp_r) or {}
        if comp_r.status_code == 200 and comp_body.get("success"):
            self.session.post(f"{self.BASE}/subjects/{subject_id}/achievements/unlock", json={}, timeout=TIMEOUT)
            return True
        return False

    def _complete_review(self, review_id):
        self.session.get(f"{self.BASE}/reviews/{review_id}/items", timeout=TIMEOUT)
        r = self.session.post(f"{self.BASE}/reviews/{review_id}/complete", json={}, timeout=TIMEOUT)
        return r.status_code == 200 and bool((safe_json(r) or {}).get("success"))

    def _solve_test(self, test_id, update_status):
        known_correct = {}
        tried_wrong = {}
        best_passed = False

        for rnd in range(1, 11):
            start_r = self.session.post(f"{self.BASE}/tests/{test_id}/start", json={}, timeout=TIMEOUT)
            if start_r.status_code != 200:
                return best_passed

            data = (safe_json(start_r) or {}).get("data") or {}
            test_questions = data.get("test_questions") or []
            options_dict = data.get("options") or {}
            if not test_questions:
                return False

            update_status(f"Test {test_id} | R{rnd}")
            chosen_map = {}
            for tq in test_questions:
                tq_id = tq["id"]
                q_id = str(tq["question_id"])
                opts = options_dict.get(q_id) or []
                if not opts:
                    continue
                if q_id in known_correct:
                    selected_id = known_correct[q_id]
                else:
                    bad = tried_wrong.get(q_id, set())
                    selected_id = next((o["id"] for o in opts if o["id"] not in bad), opts[0]["id"])
                chosen_map[tq_id] = (q_id, selected_id)
                self.session.put(
                    f"{self.BASE}/tests/{test_id}/questions/{tq_id}/answer",
                    json={"selected_option_id": selected_id},
                    timeout=TIMEOUT,
                )

            sub_r = self.session.post(f"{self.BASE}/tests/{test_id}/submit", json={}, timeout=TIMEOUT)
            sub_body = safe_json(sub_r) or {}
            if sub_r.status_code != 200 or not sub_body.get("success"):
                return best_passed

            sub_data = sub_body.get("data") or {}
            resp_info = sub_data.get("response") or {}
            score = resp_info.get("total_score", 0) or 0
            if resp_info.get("passed"):
                best_passed = True

            for qr in sub_data.get("question_responses") or []:
                tq_id = qr.get("test_question_id")
                if tq_id not in chosen_map:
                    continue
                q_id, opt_id = chosen_map[tq_id]
                if qr.get("correct_option_id") is not None:
                    known_correct[q_id] = qr["correct_option_id"]
                elif qr.get("is_correct"):
                    known_correct[q_id] = opt_id
                else:
                    tried_wrong.setdefault(q_id, set()).add(opt_id)

            if score >= 100:
                return True
            time.sleep(0.3)

        return best_passed

    def run_all(self):
        for sub in self.subjects:
            sid = sub["id"]
            label = sub.get("name") or f"Financial Literacy ({sid})"
            self.session.headers.update({"Referer": f"{self.ORIGIN}/subjects/{sid}/curriculum"})
            self.session.post(f"{self.BASE}/subjects/{sid}/media-cookie", json={}, timeout=TIMEOUT)

            curriculum = self._get_curriculum(sid)
            all_items = [item for unit in curriculum for item in (unit.get("items") or [])]
            total = len(all_items)
            completed = sum(1 for item in all_items if item.get("is_completed"))

            with make_progress() as progress:
                task = progress.add_task(label, total=max(total, 1), completed=completed, status="Starting")
                while True:
                    curriculum = self._get_curriculum(sid)
                    if not curriculum:
                        break
                    all_items = [item for unit in curriculum for item in (unit.get("items") or [])]
                    done_count = sum(1 for item in all_items if item.get("is_completed"))
                    progress.update(task, total=max(len(all_items), 1), completed=done_count)

                    next_item = None
                    unit_num = 0
                    for idx, unit in enumerate(curriculum, 1):
                        for item in unit.get("items") or []:
                            if item.get("is_unlocked") and not item.get("is_completed"):
                                next_item = item
                                unit_num = unit.get("unit_order") or idx
                                break
                        if next_item:
                            break

                    if not next_item:
                        break

                    item_id = next_item["id"]
                    item_type = next_item.get("item_type", "")
                    progress.update(task, status=f"U{unit_num} | {item_type.title()}")

                    try:
                        if item_type == "chapter":
                            ok = self._solve_chapter(sid, item_id)
                        elif item_type == "review":
                            ok = self._complete_review(item_id)
                        elif item_type == "test":
                            ok = self._solve_test(item_id, lambda st: progress.update(task, status=f"U{unit_num} | {st}"))
                        else:
                            break
                    except KeyboardInterrupt:
                        raise
                    except requests.exceptions.Timeout:
                        progress.update(task, status="Timeout, retrying")
                        time.sleep(1)
                        continue
                    except Exception:
                        break

                    if not ok:
                        progress.update(task, status="Stopped")
                        break
                    time.sleep(0.1)

                final_items = [item for unit in self._get_curriculum(sid) for item in (unit.get("items") or [])]
                progress.update(
                    task,
                    total=max(len(final_items), 1),
                    completed=sum(1 for item in final_items if item.get("is_completed")),
                    status="Done",
                )

def run_spx(student_id, password):
    spx = SPXClient()
    with console.status("[bold cyan]Signing in to Financial Literacy...[/]"):
        spx.login(student_id, password)
    if not spx.subjects:
        console.print("[bold yellow]No Financial Literacy subjects found.[/]")
        return
    spx.run_all()

# ============================================================
# التعديل الطفيف عشان البوت يقدر يشغلها من غير ما تسأل المستخدم
# ============================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--subject", required=True, choices=["1", "2", "3", "4"])
    args = parser.parse_args()

    student_id = args.id
    password = args.password
    choice = args.subject

    console.print(Panel.fit("[bold cyan]AUTO SOLVER[/]", border_style="bright_blue"))

    try:
        qureo = QureoClient()
        with console.status("[bold cyan]Signing in...[/]"):
            qureo.login(student_id, password)

        grade_two = qureo.is_grade_two()
        grade_label = "Grade 2" if grade_two else "Grade 1"
        console.print(f"[bold green]Logged in[/] | [bold magenta]{grade_label}[/]\n")

        if not grade_two:
            js_course = qureo.find_course("javascript") or (qureo.courses[0] if qureo.courses else None)
            if not js_course:
                console.print("[bold red]FATAL_ERROR: JavaScript course not found.[/]")
                sys.exit(1)
            qureo.run_course(js_course, "JavaScript")
        else:
            if choice in ("1", "4"):
                js_course = qureo.find_course("javascript")
                if js_course:
                    qureo.run_course(js_course, "JavaScript")
                else:
                    console.print("[bold yellow]WARNING: JavaScript course not found.[/]")

            if choice in ("2", "4"):
                py_course = qureo.find_course("python")
                if py_course:
                    qureo.run_course(py_course, "Python")
                else:
                    console.print("[bold yellow]WARNING: Python course not found.[/]")

            if choice in ("3", "4"):
                run_spx(student_id, password)

        console.print("\n[bold bright_green]SUCCESS[/]")
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Stopped.[/]")
        sys.exit(0)
    except Exception as exc:
        # طبع الخطأ بصيغة واضحة عشان البوت يمسكه
        console.print(f"\n[bold red]FATAL_ERROR:[/] {exc}")
        sys.exit(1)
