"""ETP 批量 Submit 服务 - 读取每条任务已有的标注答案，原样提交，不做任何修改"""
import requests
import asyncio
from typing import AsyncGenerator, Optional
from ..utils.logger import get_logger

logger = get_logger(__name__)

ETP_BASE = "https://etp-backend.momenta.works"
requests.packages.urllib3.disable_warnings()


def _parse_url(task_url: str) -> dict:
    import urllib.parse
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(task_url).query)
    return {
        "batch":    qs.get("batch",    [""])[0],
        "tag_type": qs.get("tag_type", [""])[0],
    }


def etp_login(username: str, password: str) -> dict:
    r = requests.post(f"{ETP_BASE}/api/v2/login",
                      json={"username": username, "password": password},
                      timeout=15, verify=False)
    r.raise_for_status()
    d = r.json()
    if d.get("status") != 0 and d.get("code") != 0:
        raise ValueError(f"登录失败: {d.get('message', d)}")
    data = d.get("data", {})
    return {"token": data.get("token", ""), "tagger_id": str(data.get("id", ""))}


def get_batch_info(token: str, batch: str, tag_type: str) -> dict:
    h = {"authorization": token}
    r = requests.get(f"{ETP_BASE}/api/v2/batchstatus",
                     params={"tasktype": tag_type, "batch_name": batch},
                     headers=h, timeout=15, verify=False)
    return r.json().get("data", {}) if r.ok else {}


def get_questionnaire(token: str, tag_type: str) -> dict:
    h = {"authorization": token}
    r = requests.get(f"{ETP_BASE}/api/v2/task/getTypes",
                     params={"task_mode": "common"}, headers=h, timeout=15, verify=False)
    types = r.json().get("data", {})
    if isinstance(types, dict):
        types = []
    tag_type_id = ""
    for t in (types or []):
        if t.get("name") == tag_type:
            tag_type_id = str(t.get("id", ""))
            break
    return {"tag_type_id": tag_type_id}


def _build_answers_from_questionnaire(questionnaire: dict) -> Optional[dict]:
    """
    从 getOne 返回的 questionnaire 中提取已选答案，构建 answers 对象。
    如果没有任何选中项，返回 None（说明该任务尚未标注）。
    """
    if not questionnaire:
        return None

    questions_data = questionnaire.get("questions", [])
    selected_questions = []
    tags = []

    for q in questions_data:
        q_id   = q.get("question_id")
        q_key  = q.get("question_key", "")
        q_type = q.get("type", "single_choice")
        options = q.get("options", [])

        # 找已选中的 option
        # 先看 selected 字段，再看 question_answer
        chosen = [opt for opt in options if opt.get("selected")]
        if not chosen:
            # 也试 question_answer 字段
            qa = q.get("question_answer", [])
            if qa:
                qa_ids = {a.get("option_id") for a in qa}
                chosen = [opt for opt in options if opt.get("id") in qa_ids]

        if not chosen:
            continue  # 该题未填

        for opt in chosen:
            tag = opt.get("tag") or opt.get("key") or ""
            if tag:
                tags.append(tag)

        selected_questions.append({
            "question_id":   q_id,
            "question_key":  q_key,
            "question_type": q_type,
            "options": [
                {
                    "id":    opt.get("id"),
                    "label": opt.get("label", ""),
                    "key":   opt.get("tag") or opt.get("key") or "",
                    "key2":  opt.get("tag") or opt.get("key") or "",
                }
                for opt in chosen
            ],
        })

    if not selected_questions:
        return None  # 未标注

    return {"questions": selected_questions, "tags": tags}


async def batch_submit(
    task_url: str,
    username: str,
    password: str,
) -> AsyncGenerator[dict, None]:
    """
    批量提交 ETP 任务。
    逐条调用 getOne，读取任务现有的标注答案（selected 字段），原样提交，不作任何修改。
    """
    parsed = _parse_url(task_url)
    batch    = parsed["batch"]
    tag_type = parsed["tag_type"]

    if not batch or not tag_type:
        yield {"type": "error", "message": f"无法解析 URL，batch={batch!r} tag_type={tag_type!r}"}
        return

    yield {"type": "progress", "message": "登录 ETP...", "done": 0, "total": 0}
    try:
        login = await asyncio.to_thread(etp_login, username, password)
    except Exception as e:
        yield {"type": "error", "message": f"登录失败：{e}"}
        return

    token = login["token"]
    h = {"authorization": token, "Content-Type": "application/json"}

    # 查询总数
    info = await asyncio.to_thread(get_batch_info, token, batch, tag_type)
    total_pending = int(info.get("to_be_tagged", 0)) + int(info.get("tagging", 0))
    yield {"type": "progress",
           "message": f"待提交：{total_pending} 条（仅提交已标注的，未标注的跳过）",
           "done": 0, "total": total_pending}

    params = {"tagger_id": "6793", "tag_type": tag_type,
              "batch": batch, "sub_batch": "", "share_code": ""}

    submitted    = 0
    skipped      = 0
    failed       = 0
    empty_streak = 0

    while True:
        try:
            r2 = await asyncio.to_thread(
                lambda: requests.get(f"{ETP_BASE}/api/v2/task/getOne",
                                     params=params, headers=h, timeout=15, verify=False)
            )
            task = r2.json() if r2.ok else {}
        except Exception as e:
            yield {"type": "error", "message": f"获取任务失败：{e}"}
            break

        bag = task.get("bag") or {}
        bag_id  = bag.get("bag_id", "")
        bag_md5 = bag.get("bag_md5", "")

        if not bag_id:
            empty_streak += 1
            if empty_streak >= 3:
                break
            await asyncio.sleep(1)
            continue
        empty_streak = 0

        # 读取该任务的现有标注答案
        questionnaire = task.get("questionnaire") or {}
        answers = _build_answers_from_questionnaire(questionnaire)

        if answers is None:
            skipped += 1
            yield {"type": "progress",
                   "message": f"⏭ 跳过（未标注）bag_id={bag_id}",
                   "done": submitted, "total": total_pending}
            # 未标注任务跳过，但任务已被 getOne 取出（占用了队列位置）
            # 需要重新入队或停止
            # 为避免死循环，遇到未标注就停止本次批量
            break

        q_id  = questionnaire.get("questionnaire_id")
        q_ver = questionnaire.get("questionnaire_version")

        body = {
            "tag_type":              tag_type,
            "tagger_id":             "6793",
            "bag_id":                bag_id,
            "bag_md5":               bag_md5,
            "task_type_id":          None,
            "questionnaire_id":      q_id,
            "questionnaire_version": q_ver,
            "answers":               answers,
        }

        try:
            r3 = await asyncio.to_thread(
                lambda: requests.post(f"{ETP_BASE}/api/v2/task/submit",
                                      json=body, headers=h, timeout=15, verify=False)
            )
            resp = r3.json()
            if resp.get("status") == 0 or resp.get("code") == 0:
                submitted += 1
                yield {"type": "progress",
                       "message": f"已提交 {submitted} 条，答案：{answers.get('tags', [])}",
                       "done": submitted, "total": total_pending}
            else:
                failed += 1
                logger.warning(f"submit failed bag_id={bag_id}: {resp}")
                yield {"type": "progress",
                       "message": f"⚠ bag_id={bag_id} 提交失败: {resp.get('message','')}",
                       "done": submitted, "total": total_pending}
        except Exception as e:
            failed += 1
            logger.warning(f"submit error bag_id={bag_id}: {e}")

        await asyncio.sleep(0.2)

    final_info = await asyncio.to_thread(get_batch_info, token, batch, tag_type)
    yield {
        "type": "completed",
        "message": f"完成！提交 {submitted} 条，跳过（未标注）{skipped} 条，失败 {failed} 条",
        "done": submitted,
        "total": submitted + failed + skipped,
        "batch_info": final_info,
    }
