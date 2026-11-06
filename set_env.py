import os
from pathlib import Path

# 部署者显式配置优先；默认缓存随项目目录移动。
_cache = os.environ.setdefault('HF_HOME', str(Path(__file__).resolve().parent / 'models_cache'))
os.environ.setdefault('TRANSFORMERS_CACHE', _cache)
os.environ.setdefault('HUGGINGFACE_HUB_CACHE', _cache)
