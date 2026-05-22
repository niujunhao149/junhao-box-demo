"""文本清洗工具 - 去除语气词和口语填充"""
import re


def clean_description(text: str) -> str:
    """
    保守清洗事件描述中的语气词和口语填充

    规则：
    - 删除开头/中间的语气词：嗯、啊、呃、哎、那、哦、嘿
    - 删除无意义的口语填充：那个、就是、然后（作为语气助词时）、是吧、对吧、好吧、嗯哼
    - 删除句尾不完整的片段
    - 删除连续重复超过 2 次的字符（垃圾数据）
    - 不改动技术描述、动作描述、数字、专有名词
    - 不补充任何原文没有的内容

    Args:
        text: 原始描述文本

    Returns:
        清洗后的文本
    """
    if not text:
        return text

    # 删除开头的语气词
    text = re.sub(r'^[嗯啊呃哎哦嘿]+[，,。\s]*', '', text)
    text = re.sub(r'^[那这][个么]?[，,\s]+', '', text)

    # 删除中间的语气词
    text = re.sub(r'[嗯啊呃哎哦][，,]', '', text)

    # 删除句尾的悬空部分
    text = re.sub(r'[，,][然后我这那其实就是好吧是吧对吧]+$', '', text)

    # 删除连续重复超过 2 次的字符（垃圾数据如"数数数数..."）
    text = re.sub(r'(\S)\1{2,}', r'\1', text)

    # 清理多余逗号
    text = re.sub(r'[，,]{2,}', '，', text)
    text = text.strip('，, ')

    return text.strip()
