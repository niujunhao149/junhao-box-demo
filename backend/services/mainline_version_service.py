"""主线版本更新服务 - 自动从飞书父目录找新版本并提取 x86 产物包"""
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Callable, Optional
from pathlib import Path

from ..utils.logger import get_logger

logger = get_logger(__name__)


def _extract_devcar_from_md(md: str) -> str:
    """从主线发布文档 Markdown 中提取 X86 产物包 Devcar .tgz 文件名"""
    lines = md.splitlines()
    for line in lines:
        if 'X86产物包地址' in line or 'x86产物包地址' in line:
            m = re.search(r'(Devcar-[^\s\[\]()]+\.tgz)', line)
            if m:
                return m.group(1)
    # fallback: 全文第一个 Devcar mainstream .tgz
    m = re.search(r'(Devcar-[^\s\]]+mainstream[^\s\]]*\.tgz)', md)
    return m.group(1) if m else ''


def _clean_title(raw_title: str) -> str:
    """ML6.0_EBM-R6.0_260318_B 主线软件版本发布【...】 → ML6.0_EBM-R6.0_260318_B"""
    return re.split(r'\s+主线|\s+软件', raw_title)[0].strip()


def _parse_date(title: str) -> str:
    """ML6.0_EBM-R6.0_260318_B → 0318"""
    m = re.search(r'_26(\d{4})[_\s]', title)
    return m.group(1) if m else ''


def _check_one_branch(branch: str, ref_url: str, current_dates: List[str],
                      title_pattern: str, on_progress: Optional[Callable]) -> List[dict]:
    """检查单个分支（R6 或 R7）的新版本，可并行调用"""
    import feishu_sync  # type: ignore
    sync = feishu_sync.FeishuSync(token_provider=True)

    if on_progress:
        on_progress(f"获取 {branch.upper()} 父节点...")

    info_result = sync.info_page(ref_url, force_refresh=True)
    info = info_result[1] if isinstance(info_result, tuple) else info_result
    parent_token = info.get('parent_node_token', '')
    if not parent_token:
        logger.warning(f"No parent_node_token for {ref_url}")
        return []

    parent_url = f"https://momenta.feishu.cn/wiki/{parent_token}"

    if on_progress:
        on_progress(f"获取 {branch.upper()} 版本列表...")

    pages_result = sync.list_pages(parent_url)
    pages = pages_result[1] if isinstance(pages_result, tuple) else pages_result
    if not isinstance(pages, list):
        pages = []

    latest_date = max(current_dates) if current_dates else '0000'
    logger.info(f"{branch.upper()} latest_date={latest_date}, total pages={len(pages)}")

    new_pages = []
    for page in pages:
        title = page.get('title', '')
        if not re.search(title_pattern, title):
            continue
        if '不予准出' not in title and '迭代使用' not in title:
            continue
        date = _parse_date(title)
        if not date or date <= latest_date:
            continue
        node_token = page.get('node_token', '')
        page_url = page.get('url', '') or f"https://momenta.feishu.cn/wiki/{node_token}"
        new_pages.append((date, title, page_url))

    new_pages.sort(key=lambda x: x[0])
    logger.info(f"{branch.upper()} found {len(new_pages)} new pages")

    # 串行读取，避免并发触发飞书 API 限流（99991400）
    import time as _time
    result = []
    for date, title, page_url in new_pages:
        if on_progress:
            on_progress(f"读取 {_clean_title(title)}...")
        md = ''
        for attempt in range(3):
            try:
                md = sync.read_page_as_markdown(page_url)
                break
            except Exception as e:
                if '99991400' in str(e) and attempt < 2:
                    wait = 10 * (attempt + 1)
                    logger.warning(f"Rate limited, retry in {wait}s: {page_url}")
                    _time.sleep(wait)
                else:
                    logger.warning(f"Failed to read {page_url}: {e}")
                    break
        devcar = _extract_devcar_from_md(md) if md else ''
        result.append({'date': date, 'title': _clean_title(title), 'url': page_url, 'devcar': devcar})
    return result


def check_updates(
    ref_url_r6: str,
    ref_url_r7: str,
    current_r6_dates: List[str],
    current_r7_dates: List[str],
    on_progress: Optional[Callable] = None,
) -> Dict:
    """并行检查 R6 和 R7 的新版本"""
    result = {'r6': [], 'r7': []}
    msgs = []

    def prog(msg: str):
        msgs.append(msg)
        if on_progress:
            on_progress(min(10 + len(msgs) * 5, 90), msg)

    configs = []
    if ref_url_r6:
        configs.append(('r6', ref_url_r6, current_r6_dates, r'ML6\.0_EBM-R6'))
    if ref_url_r7:
        configs.append(('r7', ref_url_r7, current_r7_dates, r'ML7\.0_EBM-R7'))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(_check_one_branch, branch, url, dates, pattern,
                            lambda msg, b=branch: prog(f"[{b.upper()}] {msg}")): branch
            for branch, url, dates, pattern in configs
        }
        for future in as_completed(futures):
            branch = futures[future]
            try:
                result[branch] = future.result()
            except Exception as e:
                logger.exception(f"{branch.upper()} check failed: {e}")

    if on_progress:
        on_progress(100, "检查完成！")
    return result


def _releases_json_path():
    return Path(__file__).parent.parent / "static" / "releases.json"


def insert_releases_to_html(r6_releases: list, r7_releases: list):
    """将新版本写入 releases.json（持久化）并更新 index.html（兼容）"""
    import json as _json

    # 1. 写 releases.json
    p = _releases_json_path()
    rdata = _json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"releases": [], "mainlineR6": [], "mainlineR7": []}
    existing_r6_urls = {r.get("url") for r in rdata["mainlineR6"]}
    existing_r7_urls = {r.get("url") for r in rdata["mainlineR7"]}
    new_r6 = [r for r in r6_releases if r.get("url") not in existing_r6_urls]
    new_r7 = [r for r in r7_releases if r.get("url") not in existing_r7_urls]
    rdata["mainlineR6"].extend(new_r6)
    rdata["mainlineR6"].sort(key=lambda r: r.get("date", ""), reverse=True)
    rdata["mainlineR7"].extend(new_r7)
    rdata["mainlineR7"].sort(key=lambda r: r.get("date", ""), reverse=True)
    if new_r6 or new_r7:
        p.write_text(_json.dumps(rdata, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"Inserted {len(new_r6)} R6 + {len(new_r7)} R7 into releases.json")

    # 2. 同时写 index.html（兼容）
    html_path = Path(__file__).parent.parent / "static" / "index.html"
    content = html_path.read_text(encoding="utf-8")
    indent = "                        "
    for releases, marker in [
        (r6_releases, "mainlineR6: [\n"),
        (r7_releases, "mainlineR7: [\n"),
    ]:
        if not releases:
            continue
        entries = ""
        for r in releases:
            entries += (
                f"{indent}{{ date:'{r['date']}', title:'{r['title']}', "
                f"url:'{r['url']}',\n"
                f"{indent}  devcar:'{r['devcar']}' }},\n"
            )
        content = content.replace(marker, marker + entries, 1)
    html_path.write_text(content, encoding="utf-8")
    logger.info(f"Inserted {len(r6_releases)} R6 + {len(r7_releases)} R7 releases into index.html")
