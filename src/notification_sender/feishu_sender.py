# -*- coding: utf-8 -*-
"""
飞书 发送提醒服务

职责：
1. 通过 webhook 发送飞书消息
"""
import base64
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from src.config import Config
from src.formatters import (
    MIN_MAX_BYTES,
    PAGE_MARKER_SAFE_BYTES,
    chunk_content_by_max_bytes,
    format_feishu_markdown,
)


logger = logging.getLogger(__name__)


class FeishuSender:
    
    def __init__(self, config: Config):
        """
        初始化飞书配置

        Args:
            config: 配置对象
        """
        self._feishu_url = getattr(config, 'feishu_webhook_url', None)
        self._feishu_secret = (getattr(config, 'feishu_webhook_secret', None) or '').strip()
        self._feishu_keyword = (getattr(config, 'feishu_webhook_keyword', None) or '').strip()
        self._feishu_max_bytes = getattr(config, 'feishu_max_bytes', 20000)
        self._webhook_verify_ssl = getattr(config, 'webhook_verify_ssl', True)
        self._feishu_app_id = (getattr(config, 'feishu_app_id', None) or '').strip()
        self._feishu_app_secret = (getattr(config, 'feishu_app_secret', None) or '').strip()
        self._feishu_chat_id = (getattr(config, 'feishu_chat_id', None) or '').strip()
        self._tenant_access_token: Optional[str] = None

    def _is_app_bot_configured(self) -> bool:
        return bool(self._feishu_app_id and self._feishu_app_secret and self._feishu_chat_id)

    def send_file_to_feishu(self, file_path: str, *, timeout_seconds: Optional[float] = None) -> bool:
        """Upload a local report file and send it to the configured Feishu chat."""
        if not self._is_app_bot_configured():
            logger.warning("飞书应用机器人未配置，无法发送文件")
            return False

        path = Path(file_path)
        if not path.is_file():
            logger.error("飞书文件发送失败，文件不存在: %s", file_path)
            return False

        token = self._get_tenant_access_token(timeout_seconds=timeout_seconds)
        if not token:
            return False

        file_key = self._upload_app_bot_file(token, path, timeout_seconds=timeout_seconds)
        if not file_key:
            return False

        return self._send_app_bot_file_message(file_key, timeout_seconds=timeout_seconds)

    def _get_keyword_prefix(self) -> str:
        """Return the keyword prefix required by Feishu webhook security settings."""
        if not self._feishu_keyword:
            return ""
        return f"{self._feishu_keyword}\n"

    def _apply_keyword_prefix(self, content: str) -> str:
        """Prepend the optional keyword so each webhook request passes keyword checks."""
        prefix = self._get_keyword_prefix()
        if not prefix:
            return content
        return f"{prefix}{content}" if content else self._feishu_keyword

    def _build_security_fields(self) -> Dict[str, str]:
        """Build optional signing fields required by Feishu custom robot security."""
        if not self._feishu_secret:
            return {}

        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{self._feishu_secret}"
        sign = base64.b64encode(
            hmac.new(
                string_to_sign.encode('utf-8'),
                digestmod=hashlib.sha256,
            ).digest()
        ).decode('utf-8')
        return {
            "timestamp": timestamp,
            "sign": sign,
        }
    
          
    def send_to_feishu(self, content: str, *, timeout_seconds: Optional[float] = None) -> bool:
        """
        推送消息到飞书机器人
        
        飞书自定义机器人 Webhook 消息格式：
        {
            "msg_type": "interactive",
            "card": {
                "config": { "wide_screen_mode": true },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": "..."
                        }
                    }
                ],
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": "A股智能分析报告"
                    }
                }
            }
        }
        
        说明：飞书文本消息不会渲染 Markdown，需使用交互卡片（lark_md）格式
        
        注意：飞书文本消息限制约 20KB，超长内容会自动分批发送
        可通过环境变量 FEISHU_MAX_BYTES 调整限制值
        
        Args:
            content: 消息内容（Markdown 会转为纯文本）
            
        Returns:
            是否发送成功
        """
        if not self._feishu_url and not self._is_app_bot_configured():
            logger.warning("飞书 Webhook 和应用机器人均未配置，跳过推送")
            return False
        
        # 飞书 lark_md 支持有限，先做格式转换
        formatted_content = format_feishu_markdown(content)

        max_bytes = self._feishu_max_bytes  # 从配置读取，默认 20000 字节
        keyword_overhead = len(self._get_keyword_prefix().encode('utf-8'))
        effective_max_bytes = max_bytes - keyword_overhead

        if effective_max_bytes <= 0:
            logger.error("飞书关键词过长，超过单条消息允许的最大字节数，无法发送")
            return False
        
        # 检查字节长度，超长则分批发送
        content_bytes = len(formatted_content.encode('utf-8')) + keyword_overhead
        if content_bytes > max_bytes:
            min_chunk_bytes = MIN_MAX_BYTES + PAGE_MARKER_SAFE_BYTES
            if effective_max_bytes < min_chunk_bytes:
                logger.error(
                    "飞书关键词过长，剩余分片预算(%s字节)不足以安全分页发送，至少需要 %s 字节",
                    effective_max_bytes,
                    min_chunk_bytes,
                )
                return False
            logger.info(f"飞书消息内容超长({content_bytes}字节/{len(content)}字符)，将分批发送")
            return self._send_feishu_chunked(formatted_content, effective_max_bytes)
        
        try:
            return self._send_feishu_message(formatted_content, timeout_seconds=timeout_seconds)
        except Exception as e:
            logger.error(f"发送飞书消息失败: {e}")
            return False
   
    def _send_feishu_chunked(self, content: str, max_bytes: int) -> bool:
        """
        分批发送长消息到飞书
        
        按股票分析块（以 --- 或 ### 分隔）智能分割，确保每批不超过限制
        
        Args:
            content: 完整消息内容
            max_bytes: 单条消息最大字节数
            
        Returns:
            是否全部发送成功
        """
        try:
            chunks = chunk_content_by_max_bytes(content, max_bytes, add_page_marker=True)
        except ValueError as e:
            logger.error("飞书消息分片失败，单片预算不足以安全分页（关键词过长或 max_bytes 过小）: %s", e)
            return False
        
        # 分批发送
        total_chunks = len(chunks)
        success_count = 0
        
        logger.info(f"飞书分批发送：共 {total_chunks} 批")
        
        for i, chunk in enumerate(chunks):
            try:
                if self._send_feishu_message(chunk):
                    success_count += 1
                    logger.info(f"飞书第 {i+1}/{total_chunks} 批发送成功")
                else:
                    logger.error(f"飞书第 {i+1}/{total_chunks} 批发送失败")
            except Exception as e:
                logger.error(f"飞书第 {i+1}/{total_chunks} 批发送异常: {e}")
            
            # 批次间隔，避免触发频率限制
            if i < total_chunks - 1:
                time.sleep(1)
        
        return success_count == total_chunks
    
    def _send_feishu_message(self, content: str, *, timeout_seconds: Optional[float] = None) -> bool:
        """发送单条飞书消息（优先使用 Markdown 卡片）"""
        prepared_content = self._apply_keyword_prefix(content)
        security_fields = self._build_security_fields()

        def _post_payload(payload: Dict[str, Any]) -> bool:
            request_payload = dict(payload)
            request_payload.update(security_fields)
            logger.debug(f"飞书请求 URL: {self._feishu_url}")
            logger.debug(f"飞书请求 payload 长度: {len(prepared_content)} 字符")

            response = requests.post(
                self._feishu_url,
                json=request_payload,
                timeout=timeout_seconds or 30,
                verify=self._webhook_verify_ssl
            )

            logger.debug(f"飞书响应状态码: {response.status_code}")
            logger.debug(f"飞书响应内容: {response.text}")

            if response.status_code == 200:
                result = response.json()
                code = result.get('code') if 'code' in result else result.get('StatusCode')
                if code == 0:
                    logger.info("飞书消息发送成功")
                    return True
                else:
                    error_msg = result.get('msg') or result.get('StatusMessage', '未知错误')
                    error_code = result.get('code') or result.get('StatusCode', 'N/A')
                    logger.error(f"飞书返回错误 [code={error_code}]: {error_msg}")
                    logger.error(f"完整响应: {result}")
                    return False
            else:
                logger.error(f"飞书请求失败: HTTP {response.status_code}")
                logger.error(f"响应内容: {response.text}")
                return False

        # 1) 优先使用交互卡片（支持 Markdown 渲染）
        card_payload = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": "股票智能分析报告"
                    }
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": prepared_content
                        }
                    }
                ]
            }
        }

        if self._send_payload(card_payload, _post_payload, timeout_seconds=timeout_seconds):
            return True

        # 2) 回退为普通文本消息
        text_payload = {
            "msg_type": "text",
            "content": {
                "text": prepared_content
            }
        }

        return self._send_payload(text_payload, _post_payload, timeout_seconds=timeout_seconds)

    def _send_payload(
        self,
        payload: Dict[str, Any],
        webhook_sender,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        if self._feishu_url and webhook_sender(payload):
            return True
        if self._is_app_bot_configured():
            return self._send_app_bot_message(payload, timeout_seconds=timeout_seconds)
        return False

    def _get_tenant_access_token(self, *, timeout_seconds: Optional[float] = None) -> Optional[str]:
        if self._tenant_access_token:
            return self._tenant_access_token

        response = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={
                "app_id": self._feishu_app_id,
                "app_secret": self._feishu_app_secret,
            },
            timeout=timeout_seconds or 30,
        )
        logger.debug("飞书 tenant_access_token 响应状态码: %s", response.status_code)
        if response.status_code != 200:
            logger.error("获取飞书 tenant_access_token 失败: HTTP %s", response.status_code)
            logger.error("响应内容: %s", response.text)
            return None

        result = response.json()
        if result.get("code") != 0:
            logger.error(
                "获取飞书 tenant_access_token 返回错误 [code=%s]: %s",
                result.get("code", "N/A"),
                result.get("msg", "未知错误"),
            )
            logger.error("完整响应: %s", result)
            return None

        token = (result.get("tenant_access_token") or "").strip()
        if not token:
            logger.error("获取飞书 tenant_access_token 成功但响应中没有 token")
            return None
        self._tenant_access_token = token
        return token

    def _send_app_bot_message(self, payload: Dict[str, Any], *, timeout_seconds: Optional[float] = None) -> bool:
        token = self._get_tenant_access_token(timeout_seconds=timeout_seconds)
        if not token:
            return False

        response = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={
                "receive_id": self._feishu_chat_id,
                "msg_type": payload["msg_type"],
                "content": json.dumps(
                    payload["card"] if payload["msg_type"] == "interactive" else payload["content"],
                    ensure_ascii=False,
                ),
            },
            timeout=timeout_seconds or 30,
        )

        logger.debug("飞书应用机器人响应状态码: %s", response.status_code)
        logger.debug("飞书应用机器人响应内容: %s", response.text)
        if response.status_code != 200:
            logger.error("飞书应用机器人请求失败: HTTP %s", response.status_code)
            logger.error("响应内容: %s", response.text)
            return False

        result = response.json()
        if result.get("code") == 0:
            logger.info("飞书应用机器人消息发送成功")
            return True

        logger.error(
            "飞书应用机器人返回错误 [code=%s]: %s",
            result.get("code", "N/A"),
            result.get("msg", "未知错误"),
        )
        logger.error("完整响应: %s", result)
        return False

    def _upload_app_bot_file(
        self,
        token: str,
        path: Path,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> Optional[str]:
        with path.open("rb") as file_obj:
            response = requests.post(
                "https://open.feishu.cn/open-apis/im/v1/files",
                headers={"Authorization": f"Bearer {token}"},
                data={
                    "file_type": "stream",
                    "file_name": path.name,
                },
                files={"file": (path.name, file_obj, "text/markdown")},
                timeout=timeout_seconds or 60,
            )

        logger.debug("飞书文件上传响应状态码: %s", response.status_code)
        logger.debug("飞书文件上传响应内容: %s", response.text)
        if response.status_code != 200:
            logger.error("飞书文件上传失败: HTTP %s", response.status_code)
            logger.error("响应内容: %s", response.text)
            return None

        result = response.json()
        if result.get("code") != 0:
            logger.error(
                "飞书文件上传返回错误 [code=%s]: %s",
                result.get("code", "N/A"),
                result.get("msg", "未知错误"),
            )
            logger.error("完整响应: %s", result)
            return None

        file_key = (result.get("data") or {}).get("file_key")
        if not file_key:
            logger.error("飞书文件上传成功但响应中没有 file_key")
            return None
        logger.info("飞书文件上传成功: %s", path.name)
        return str(file_key)

    def _send_app_bot_file_message(self, file_key: str, *, timeout_seconds: Optional[float] = None) -> bool:
        token = self._get_tenant_access_token(timeout_seconds=timeout_seconds)
        if not token:
            return False

        response = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={
                "receive_id": self._feishu_chat_id,
                "msg_type": "file",
                "content": json.dumps({"file_key": file_key}, ensure_ascii=False),
            },
            timeout=timeout_seconds or 30,
        )

        logger.debug("飞书文件消息响应状态码: %s", response.status_code)
        logger.debug("飞书文件消息响应内容: %s", response.text)
        if response.status_code != 200:
            logger.error("飞书文件消息请求失败: HTTP %s", response.status_code)
            logger.error("响应内容: %s", response.text)
            return False

        result = response.json()
        if result.get("code") == 0:
            logger.info("飞书文件消息发送成功")
            return True

        logger.error(
            "飞书文件消息返回错误 [code=%s]: %s",
            result.get("code", "N/A"),
            result.get("msg", "未知错误"),
        )
        logger.error("完整响应: %s", result)
        return False
