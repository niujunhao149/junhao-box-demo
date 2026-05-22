#!/usr/bin/env python3
"""
从 Harz weekly release 飞书文档的 Markdown 内容中提取版本信息。

用法:
    feishu-sync-cli read_page_as_markdown <URL> 2>/dev/null | python3 extract_versions.py

输出 JSON:
{
  "date": "0312",
  "st35_master": "HarzQNX-V6.0.39-ST35_A920_DEV_MF_3118-20260312.512.tgz",
  "st35_weekly": "HarzQNX-V6.0.39-ST35_A082.24_DEV_MF_3504-20260312.4696.tgz",
  "st3_master":  "HarzQNX-V6.0.39-ST3_V920_DEV_MF_487-20260312.507.tgz",
  "st3_weekly":  "HarzQNX-V6.0.39-ST3_A182.24_DEV_MF_148-20260312.6945.tgz",
  "devcar_7v":   "Devcar-V6.0.39-mainstream_MAX_SELECTOR_cuda11_421824-20260312.515.tgz"
}
"""

import sys
import re
import json


def extract_td3(section_text: str, row_keyword: str) -> str:
    """
    在 section_text 中找到第一行包含 row_keyword 的 <tr>，
    返回该行第三个 <td> 的内容（去掉 HTML 标签）。
    """
    for line in section_text.splitlines():
        if row_keyword not in line:
            continue
        # 提取所有 <td>...</td> 内容
        cells = re.findall(r'<td>(.*?)</td>', line)
        if len(cells) >= 3:
            raw = cells[2].strip()
            # 去掉 markdown 加粗等修饰
            raw = re.sub(r'\*\*|__', '', raw)
            # 只取 .tgz 文件名（可能后面跟了备注）
            m = re.search(r'(\S+\.tgz)', raw)
            if m:
                filename = m.group(1)
                if 'shadowmode' in filename.lower():
                    continue  # 跳过 shadow mode 包
                return filename
            return raw
    return ""


def parse(content: str) -> dict:
    # ── 日期：从标题 [0312] 提取 ──────────────────────────────────────
    date = ""
    m = re.search(r'#\s*\[(\d{4})\]', content)
    if m:
        date = m.group(1)

    # ── 按 section 分割 ───────────────────────────────────────────────
    # 找各 section 的起始位置
    def section_content(pattern: str) -> str:
        """返回从 pattern 匹配处到下一个 ## 之间的文本"""
        m = re.search(pattern, content, re.IGNORECASE)
        if not m:
            return ""
        start = m.start()
        # 找下一个 ## 标题
        next_section = re.search(r'\n##\s', content[start + 1:])
        end = start + 1 + next_section.start() if next_section else len(content)
        return content[start:end]

    st35_master_sec  = section_content(r'##\s*ST35 master\s+')
    st35_weekly_sec  = section_content(r'##\s*ST35\s+A0\d+\.\d+\s+weekly release')
    st3_master_sec   = section_content(r'##\s*ST3 master\s+')
    st3_weekly_sec   = section_content(r'##\s*ST3\s+A1\d+\.\d+\s+weekly release')
    devcar_sec       = section_content(r'##\s*通用产物包信息')

    # ── 提取各字段 ───────────────────────────────────────────────────
    st35_master = extract_td3(st35_master_sec,  'ST35')
    st35_weekly = extract_td3(st35_weekly_sec,  'ST35')
    st3_master  = extract_td3(st3_master_sec,   'ST3')
    st3_weekly  = extract_td3(st3_weekly_sec,   'ST3')
    # Devcar EBM 7V：通用产物包信息 section 的标题是纯中文（可能乱码），
    # 直接在全文中找含 system_7v3r 的行（该路径在文档中唯一）
    devcar_7v = extract_td3(content, 'system_7v3r')
    if not devcar_7v:
        devcar_7v = extract_td3(devcar_sec, 'EBM 7v')

    return {
        "date":        date,
        "st35_master": st35_master,
        "st35_weekly": st35_weekly,
        "st3_master":  st3_master,
        "st3_weekly":  st3_weekly,
        "devcar_7v":   devcar_7v,
    }


if __name__ == "__main__":
    content = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    result = parse(content)

    # 打印结果
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # 检查是否有空字段
    missing = [k for k, v in result.items() if not v]
    if missing:
        print(f"\n⚠️  以下字段未能提取，请手动检查: {missing}", file=sys.stderr)
    else:
        print("\n✅ 所有字段提取成功", file=sys.stderr)
