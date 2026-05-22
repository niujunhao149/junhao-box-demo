"""
SimICT 原包 & 仿真 Mviz 匹配服务
端口自 ~/.claude/skills/sim-bag-match/scripts/sim_bag_match.py
"""
import sys
import re
import json
import time
import datetime
import requests
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from typing import Callable

# ── Keycloak auth ──────────────────────────────────────────────────────────
from .keycloak_auth import get_token as _get_keycloak_token


# ── Config ─────────────────────────────────────────────────────────────────
SIMICT_BASE       = "https://simulation.momenta.works/portal"
ESS_BASE          = "https://ess.momenta.works"
FEISHU_FOLDER     = "JXTWfou8jlGcgldkFjocOABan0c"
COL_WIDTHS        = [40, 200, 200, 160, 160]
HEADER_COLS       = ["#", "Scenario Name", "Event Name", "原包 Mviz", "仿真 Mviz"]
SLEEP_API         = 0.15
SLEEP_TABLE       = 0.4


# ── Feishu token ───────────────────────────────────────────────────────────
def _get_feishu_token() -> str:
    from feishu_sync.retoken import get_access_token
    token, _ = get_access_token()
    return token


# ── Feishu Doc ─────────────────────────────────────────────────────────────
class _FeishuDoc:
    def __init__(self, doc_id: str, token: str):
        self.doc_id  = doc_id
        self.base    = f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}"
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _api(self, method, path, **kw):
        r = requests.request(method, self.base + path, headers=self.headers, timeout=20, **kw)
        return r.json()

    def append_blocks(self, children):
        return self._api("POST", f"/blocks/{self.doc_id}/children", json={"children": children})

    def create_table(self, rows: int, col_widths: list):
        init_rows = min(rows, 9)
        r = self.append_blocks([{"block_type": 31, "table": {
            "property": {"row_size": init_rows, "column_size": len(col_widths), "column_width": col_widths}
        }}])
        table_id = r.get("data", {}).get("children", [{}])[0].get("block_id", "")
        time.sleep(SLEEP_TABLE)

        current = init_rows
        while current < rows:
            self._api("PATCH", f"/blocks/{table_id}", json={"insert_table_row": {"row_index": current}})
            current += 1
            time.sleep(0.15)

        detail = self._api("GET", f"/blocks/{table_id}")
        cells  = detail.get("data", {}).get("block", {}).get("table", {}).get("cells", [])

        children_map: dict[str, list[str]] = {}
        page_token = None
        while True:
            params_blk: dict = {"page_size": "500"}
            if page_token:
                params_blk["page_token"] = page_token
            all_resp = self._api("GET", "/blocks", params=params_blk)
            for blk in all_resp.get("data", {}).get("items", []):
                pid = blk.get("parent_id", "")
                if pid:
                    children_map.setdefault(pid, []).append(blk["block_id"])
            if not all_resp.get("data", {}).get("has_more"):
                break
            page_token = all_resp["data"].get("page_token")

        return [(children_map.get(c) or [None])[0] for c in cells]

    def fill_cell(self, text_block_id, elements: list):
        if text_block_id:
            self._api("PATCH", f"/blocks/{text_block_id}",
                      json={"update_text_elements": {"elements": elements}})

    @staticmethod
    def text(content: str, bold: bool = False) -> dict:
        e = {"text_run": {"content": content}}
        if bold:
            e["text_run"]["text_element_style"] = {"bold": True}
        return e

    @staticmethod
    def link(label: str, url: str) -> dict:
        return {"text_run": {"content": label, "text_element_style": {"link": {"url": url}}}}


def _create_doc(title: str):
    token = _get_feishu_token()
    r = requests.post(
        "https://open.feishu.cn/open-apis/docx/v1/documents",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"title": title, "folder_token": FEISHU_FOLDER},
        timeout=15
    )
    resp = r.json()
    if resp.get("code") != 0:
        raise RuntimeError(f"create doc failed: {resp}")
    return resp["data"]["document"]["document_id"], token


# ── URL parsing ────────────────────────────────────────────────────────────
def _parse_input(raw: str):
    raw = raw.strip()
    if raw.startswith("http"):
        parsed = urlparse(raw)
        qs         = parse_qs(parsed.query)
        categories       = qs.get("categories", [])
        checkers         = qs.get("failed_checkers", [])
        passed_checkers  = qs.get("passed_checkers", [])
        path       = parsed.path.rstrip("/")
        batch_m = re.search(r"/batch/([a-f0-9]{24})", path)
        task_m  = re.search(r"/task/([a-f0-9]{24})", path)
        if batch_m:
            return "batch", batch_m.group(1), categories, checkers, passed_checkers
        elif task_m:
            return "task", task_m.group(1), categories, checkers, passed_checkers
        raise ValueError(f"Cannot parse batch/task ID from: {raw}")
    if re.fullmatch(r"[a-f0-9]{24}", raw):
        return "batch", raw, [], [], []
    raise ValueError(f"Invalid input: {raw}")


# ── Main service ───────────────────────────────────────────────────────────
class SimBagService:
    """
    run() runs synchronously (call via asyncio.to_thread).
    on_progress(progress: int, message: str) is called throughout.
    Returns dict: {feishu_url, doc_title, event_count}
    """

    def run(self, batch_url: str, on_progress: Callable) -> dict:
        on_progress(5, "正在解析输入...")
        input_type, input_id, categories, checkers, passed_checkers = _parse_input(batch_url)

        on_progress(10, "正在获取 Keycloak token...")
        token = _get_keycloak_token()
        auth_headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # ── Step 1: get tasks ────────────────────────────────────────────
        on_progress(15, "正在获取 SimICT task 列表...")
        if input_type == "batch":
            r = requests.get(f"{SIMICT_BASE}/api/v1/task/batch/list",
                             headers=auth_headers, params={"id": input_id, "size": 1}, timeout=20)
            r.raise_for_status()
            batch_list = r.json()
            if not batch_list:
                raise ValueError(f"Batch {input_id} not found")
            batch_name = batch_list[0].get("name", input_id)

            r2 = requests.get(f"{SIMICT_BASE}/api/v1/task/batch/{input_id}/task_list",
                              headers=auth_headers, timeout=20)
            r2.raise_for_status()
            data2 = r2.json()
            tasks = data2 if isinstance(data2, list) else (data2.get("list") or data2.get("items") or [])
        else:
            tasks     = [{"id": input_id}]
            batch_name = f"Task_{input_id[:12]}"

        # ── Step 2: get event runs ───────────────────────────────────────
        if input_type == "batch" and (categories or checkers or passed_checkers):
            # 新方案：tree + query，绕过 ES 分页限制
            on_progress(20, "正在通过 tree 接口获取筛选后的事件 ID...")
            params = {"batch_id": input_id, "bind_event_run_ids": "true"}
            if categories:
                params["categories"] = categories
            if checkers:
                params["failed_checkers"] = checkers
            if passed_checkers:
                params["passed_checkers"] = passed_checkers
            r = requests.get(f"{SIMICT_BASE}/task/batch/{input_id}/tree",
                             headers=auth_headers, params=params, timeout=30)
            r.raise_for_status()
            tree = r.json()

            def collect_ids(node):
                ids = list(node.get("event_run_ids") or [])
                for child in (node.get("children") or {}).values():
                    ids.extend(collect_ids(child))
                return ids

            event_run_ids = collect_ids(tree)
            if not event_run_ids:
                raise ValueError("没有匹配的 event，请检查筛选条件")

            on_progress(35, f"正在查询 {len(event_run_ids)} 个事件详情...")
            query_body = {"event_run_ids": event_run_ids,
                          "categories": categories,
                          "failed_checkers": checkers}
            if passed_checkers:
                query_body["passed_checkers"] = passed_checkers
            r2 = requests.post(f"{SIMICT_BASE}/task/event_run/query",
                               headers=auth_headers,
                               json=query_body,
                               timeout=30)
            r2.raise_for_status()
            data2 = r2.json()
            filtered = data2 if isinstance(data2, list) else (
                data2.get("events") or data2.get("list") or data2.get("items") or data2.get("data") or [])
        else:
            # 旧方案：逐页拉取（无筛选条件时）
            on_progress(20, f"正在拉取 {len(tasks)} 个 task 的 event runs...")
            all_events = []
            for task in tasks:
                task_id = task.get("id") or task.get("_id")
                page = 1
                while True:
                    r = requests.get(
                        f"{SIMICT_BASE}/api/v2/task/{task_id}/event_run_report",
                        headers=auth_headers, params={"page": page, "size": 500}, timeout=30
                    )
                    r.raise_for_status()
                    d = r.json()
                    items = d.get("list") or []
                    total = d.get("total", 0)
                    all_events.extend(items)
                    if len(all_events) >= total or not items:
                        break
                    page += 1
            filtered = all_events

        if not filtered:
            raise ValueError("没有匹配的 event，请检查筛选条件")

        # ── Step 3: get ESS info for each event ─────────────────────────
        rows = []
        total_n = len(filtered)
        for i, e in enumerate(filtered):
            progress_pct = 25 + int((i / total_n) * 60)
            on_progress(progress_pct, f"正在查询 ESS 原包信息 ({i+1}/{total_n})...")

            event_id = e.get("event_id", "")
            # 兼容新格式（mviz_result.base_url）和旧格式（stage_reports）
            mviz_result = e.get("mviz_result")
            if mviz_result and mviz_result.get("base_url"):
                sim_mviz = mviz_result["base_url"]
            else:
                sim_mviz = ""
                for sr in (e.get("stage_reports") or []):
                    if sr.get("type") == "simulator" and sr.get("mviz_url"):
                        sim_mviz = sr["mviz_url"]
                        break

            event_name = bag_mviz = ""
            # 兼容新格式 event_url 和旧格式
            ess_link = e.get("event_url") or (f"https://ess.momenta.works/ui/event/{event_id}" if event_id else "")

            if event_id:
                try:
                    resp = requests.get(f"{ESS_BASE}/api/v1/event/{event_id}",
                                        headers=auth_headers, timeout=20)
                    resp.raise_for_status()
                    info = resp.json().get("info", {})
                    event_name = info.get("name", "")
                    bag_infos  = info.get("bag_infos") or []
                    bag_mviz   = bag_infos[0].get("mviz_link", "") if bag_infos else ""
                except Exception:
                    pass
                time.sleep(SLEEP_API)

            rows.append({
                "scenario_name": e.get("scenario_name") or e.get("name", ""),
                "event_name":    event_name or e.get("scenario_name") or e.get("name", ""),
                "ess_link":      ess_link,
                "bag_mviz":      bag_mviz,
                "sim_mviz":      sim_mviz,
            })

        # ── Step 4: create Feishu doc ────────────────────────────────────
        on_progress(87, "正在创建飞书文档...")
        date_str     = datetime.datetime.now().strftime("%Y%m%d_%H%M")
        checkers_str = "+".join(checkers + passed_checkers) if (checkers or passed_checkers) else "all"
        doc_title    = f"sim_match_{checkers_str}_{date_str}"

        source_url = batch_url if batch_url.startswith("http") else (
            f"https://simulation.momenta.works/batch/{input_id}" if input_type == "batch"
            else f"https://simulation.momenta.works/task/{input_id}"
        )

        doc_id, feishu_token = _create_doc(doc_title)
        doc = _FeishuDoc(doc_id, feishu_token)
        doc_url = f"https://momenta.feishu.cn/docx/{doc_id}"

        doc.append_blocks([
            {"block_type": 3, "heading1": {"elements": [{"text_run": {"content": doc_title}}], "style": {}}},
            {"block_type": 2, "text": {"elements": [doc.text("source: "), doc.link(source_url, source_url)], "style": {}}},
            {"block_type": 2, "text": {"elements": [
                doc.text(f"filter: {' / '.join(categories + checkers + passed_checkers) or 'none'}  |  {len(rows)} events")
            ], "style": {}}},
        ])

        on_progress(92, f"正在写入表格 ({len(rows)} 行)...")
        ncols = len(COL_WIDTHS)
        cells = doc.create_table(1 + len(rows), COL_WIDTHS)

        for ci, hdr in enumerate(HEADER_COLS):
            doc.fill_cell(cells[ci], [doc.text(hdr, bold=True)])

        for i, row in enumerate(rows):
            base = (i + 1) * ncols
            doc.fill_cell(cells[base],     [doc.text(str(i + 1))])
            doc.fill_cell(cells[base + 1], [doc.text(row["scenario_name"])])
            doc.fill_cell(cells[base + 2],
                          [doc.link(row["event_name"], row["ess_link"])] if row["ess_link"] else [doc.text(row["event_name"])])
            doc.fill_cell(cells[base + 3],
                          [doc.link("原包 Mviz", row["bag_mviz"])] if row["bag_mviz"] else [doc.text("—")])
            doc.fill_cell(cells[base + 4],
                          [doc.link("仿真 Mviz", row["sim_mviz"])] if row["sim_mviz"] else [doc.text("—")])
            if i % 5 == 0:
                time.sleep(0.05)

        on_progress(100, "完成！")
        return {"feishu_url": doc_url, "doc_title": doc_title, "event_count": len(rows)}
