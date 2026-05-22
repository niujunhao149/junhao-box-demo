"""Harz 每周发版 - 版本信息提取与版本表更新"""
import re
import json
from typing import Optional, Callable, List, Dict

from ..utils.logger import get_logger

logger = get_logger(__name__)


# 版本表 wiki URL（用户维护的 发版-harz 表格）
WIKI_URL = "https://momenta.feishu.cn/wiki/PBLrw4867iwRUOkocd0cTwrpnkg"

# 版本表中的空行（用作插入新行的锚点）
EMPTY_ROW = "<tr><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>"


def get_package_list(branch: str = "harz") -> Dict[str, List[Dict]]:
    """
    从飞书版本表中获取可用的 DevCar 包列表。

    Args:
        branch: "harz" 或 "mainline"

    Returns:
        {
            "harz": [
                {"name": "Devcar-V6.0.39-...-20260326.1943.tgz", "date": "20260326"},
                ...
            ],
            "mainline": [
                ...
            ]
        }
    """
    import feishu_sync  # type: ignore

    try:
        sync = feishu_sync.FeishuSync(token_provider=True)

        # 读取版本表（强制刷新获取最新数据）
        md = sync.read_page_as_markdown(WIKI_URL, force_refresh=True)

        logger.info(f"Fetched package list from wiki, markdown length: {len(md)}")

        # 解析 Markdown 表格中的 tgz 包名
        # 查找所有 Devcar-*.tgz 的包名
        pattern = r'(Devcar-V[\d.]+[^|\s]*\.tgz)'
        matches = re.findall(pattern, md)

        # 去重并按日期排序（最新的在前）
        packages = list(set(matches))

        # 区分 Harz 和主线
        result = {
            "harz": [],
            "mainline": []
        }

        for pkg in packages:
            item = {
                "name": pkg,
                "date": extract_date_from_package(pkg)
            }

            # 根据包名判断分支
            if "harz" in pkg.lower():
                result["harz"].append(item)
            else:
                result["mainline"].append(item)

        # 分别按日期排序（最新的在前）
        result["harz"].sort(key=lambda x: x["date"], reverse=True)
        result["mainline"].sort(key=lambda x: x["date"], reverse=True)

        logger.info(f"Package list: harz={len(result['harz'])}, mainline={len(result['mainline'])}")
        if result["mainline"]:
            logger.info(f"Mainline first 3: {[p['date'] for p in result['mainline'][:3]]}")
        return result

    except Exception as e:
        logger.exception(f"Failed to get package list: {e}")
        return {"harz": [], "mainline": []}


def extract_date_from_package(package_name: str) -> str:
    """从包名中提取日期，格式如 20260326"""
    match = re.search(r'(\d{8})\.', package_name)
    if match:
        return match.group(1)
    return ""


def extract_from_doc(release_url: str) -> dict:
    """
    从 Harz weekly release 飞书文档中提取版本信息。

    Returns:
        {
            "date": "0326",
            "st35_master": "HarzQNX-V6.0.39-ST35_A920_...-20260326.1922.tgz",
            "st35_weekly": "HarzQNX-V6.0.39-ST35_A082.26_...-20260326.1948.tgz",
            "st3_master":  "HarzQNX-V6.0.39-ST3_V920_...-20260326.1918.tgz",
            "st3_weekly":  "HarzQNX-V6.0.39-ST3_A182.26_...-20260326.1924.tgz",
            "devcar_7v":   "Devcar-V6.0.39-mainstream_...-20260326.1943.tgz",
            "doc_title":   "[0326] [V6.0.39 harz_6093] ...",
            "release_url": "https://momenta.feishu.cn/docx/BAEv..."
        }
    """
    import feishu_sync  # type: ignore

    from .extract_versions import parse  # type: ignore

    sync = feishu_sync.FeishuSync(token_provider=True)

    # 获取文档标题（info_page 返回 (file_path, dict) 元组）
    info_result = sync.info_page(release_url, force_refresh=True)
    info = info_result[1] if isinstance(info_result, tuple) else info_result
    doc_title = info.get("title", "")
    logger.info(f"Doc title (raw): {repr(doc_title)}")

    # 获取 Markdown 内容
    md = sync.read_page_as_markdown(release_url, force_refresh=True)

    # 解析 .tgz 文件名
    data = parse(md)
    data["doc_title"] = doc_title
    data["release_url"] = release_url

    logger.info(f"Extracted versions: date={data['date']}, st35_master={data['st35_master'][:40] if data['st35_master'] else '(empty)'}")
    return data


def update_wiki(
    release_url: str,
    doc_title: str,
    data: dict,
    progress_callback: Optional[Callable[[int, str], None]] = None
) -> str:
    """
    在版本表 wiki 顶部插入一行新记录。

    Returns:
        wiki URL
    """
    import feishu_sync  # type: ignore

    if progress_callback:
        progress_callback(20, "正在连接飞书...")

    sync = feishu_sync.FeishuSync(token_provider=True)

    # 构建新行 HTML（14 列）
    # col 0: 集成版本链接, col 1: 日期, col 2-6: tgz, col 7-13: 留空
    new_row = (
        f'<tr>'
        f'<td>[{doc_title}]({release_url})</td>'
        f'<td>{data.get("date", "")}</td>'
        f'<td>{data.get("st35_master", "")}</td>'
        f'<td>{data.get("st35_weekly", "")}</td>'
        f'<td>{data.get("st3_master", "")}</td>'
        f'<td>{data.get("st3_weekly", "")}</td>'
        f'<td>{data.get("devcar_7v", "")}</td>'
        f'<td></td><td></td><td></td><td></td><td></td><td></td><td></td>'
        f'</tr>'
    )

    logger.info(f"Inserting new row for date={data.get('date')}")

    if progress_callback:
        progress_callback(50, "正在读取版本表...")

    # 插入到空行之后、第一条数据行之前
    old_string = EMPTY_ROW + "\n<tr><td>[["
    new_string = EMPTY_ROW + "\n" + new_row + "\n<tr><td>[["

    if progress_callback:
        progress_callback(70, "正在写入版本表...")

    result = sync.edit_page(
        WIKI_URL,
        old_string=old_string,
        new_string=new_string,
    )

    logger.info(f"Wiki update result: {result}")

    if progress_callback:
        progress_callback(100, "版本表更新完成！")

    return WIKI_URL
