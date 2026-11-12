"""测试一律把工作区指到临时目录：绝不碰真实的设置、设计稿和会议记录。"""
import os
import tempfile

os.environ["AVATAR_WORKSPACE"] = tempfile.mkdtemp(prefix="avatar-test-")
os.environ["AVATAR_CLAUDE_HOME"] = tempfile.mkdtemp(prefix="avatar-test-claude-")
