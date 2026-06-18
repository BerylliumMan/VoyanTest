import logging

logger = logging.getLogger(__name__)


def extract_text_from_md(file) -> str:
    content = file.read()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = content.decode("gbk", errors="replace")
            logger.info("MD file decoded with GBK fallback")
        except (UnicodeDecodeError, LookupError):
            # LookupError: 未知编码名；最终回退到 UTF-8 强制替换
            text = content.decode("utf-8", errors="replace")
            logger.warning("MD file decoded with UTF-8 error replacement")
    file.seek(0)
    return text
