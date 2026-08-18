"""
FFXIV 招募板搜索系统 - 一键启动
用法：python run.py
- 自动使用 SQLite（无需安装 PostgreSQL）
- 自动安装依赖
- 启动 API 服务
- 同时弹出新的控制台窗口运行爬虫，实时显示新招募
"""

import os
import sys
import time
import urllib.request
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def main():
    print("=" * 60)
    print("  FFXIV 招募板搜索系统 - 一键启动")
    print("=" * 60)
    print()

    os.chdir(BASE_DIR)
    os.environ["DB_TYPE"] = "sqlite"
    print(f"  工作目录: {BASE_DIR}")
    print()

    # 1. 检查 Python
    print("[1/4] 检查 Python...")
    if sys.version_info < (3, 9):
        print("  ✗ 需要 Python 3.9+，当前版本:", sys.version)
        input("按回车键退出...")
        sys.exit(1)
    print(f"  ✓ Python {sys.version.split()[0]}")

    # 2. 检查依赖
    print()
    print("[2/4] 检查依赖...")
    required = {"fastapi": "fastapi", "uvicorn": "uvicorn", "httpx": "httpx"}
    missing = []
    for module, package in required.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)

    if missing:
        print(f"  安装缺失依赖: {', '.join(missing)}")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install"] + missing
        )
        print("  ✓ 依赖安装完成")
    else:
        print("  ✓ 依赖已安装")

    # 3. 启动爬虫（新控制台窗口）
    print()
    print("[3/4] 启动爬虫（新控制台窗口）...")
    scraper_path = os.path.join(BASE_DIR, "scraper.py")

    if sys.platform == "win32":
        # Windows: CREATE_NEW_CONSOLE 直接弹出新窗口
        subprocess.Popen(
            [sys.executable, scraper_path],
            cwd=BASE_DIR,
            env={**os.environ, "DB_TYPE": "sqlite"},
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    else:
        subprocess.Popen(
            ["xterm", "-title", "FFXIV Scraper", "-e", sys.executable, scraper_path],
            cwd=BASE_DIR,
            env={**os.environ, "DB_TYPE": "sqlite"},
        )
    print("  ✓ 爬虫已在新窗口启动，每 90 秒（含随机 1-3 秒延迟）抓取一次真实招募数据")
    print("    新招募会在该窗口实时显示 ✨")

    # 等待爬虫先跑一轮
    print("  等待爬虫首次抓取完成...")
    time.sleep(3)

    # 4. 启动 API 服务
    print()
    print("[4/4] 启动 API 服务...")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app",
         "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"],
        cwd=BASE_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,  # 行缓冲，确保日志实时输出
        env={**os.environ, "DB_TYPE": "sqlite", "PYTHONUNBUFFERED": "1"},
    )

    # 等待启动
    print("  等待服务启动...")
    for i in range(30):
        time.sleep(1)
        try:
            resp = urllib.request.urlopen("http://localhost:8000/health", timeout=2)
            if resp.status == 200:
                data = resp.read().decode()
                print(f"  ✓ 服务已启动 (健康检查: {data})")
                break
        except Exception:
            pass
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            print(f"  ✗ 服务启动失败！输出：")
            print("  " + "\n  ".join(out.splitlines()[-10:]))
            input("按回车键退出...")
            sys.exit(1)
    else:
        print("  ⚠ 启动超时，但进程仍在运行中...")

    # 验证数据
    print()
    print("  验证数据...")
    try:
        resp = urllib.request.urlopen("http://localhost:8000/api/v1/history?page=1&page_size=3", timeout=5)
        data = resp.read().decode()
        import json
        parsed = json.loads(data)
        print(f"  ✓ API 正常，共 {parsed['total_count']} 条记录")
        if parsed['items']:
            item = parsed['items'][0]
            print(f"    示例: [{item['datacenter']}] {item['duty_name'] or '未指定'} - {item['creator_name']}")
    except Exception as e:
        print(f"  ⚠ 数据验证失败: {e}")

    # 显示信息
    print()
    print("  ┌──────────────────────────────────────────────┐")
    print("  │  ✅ 全部就绪！                                │")
    print("  │                                              │")
    print("  │  🌐 前端页面: http://localhost:8000/         │")
    print("  │  📖 API 文档: http://localhost:8000/docs     │")
    print("  │  🕷️  爬虫监控: 已在独立窗口运行               │")
    print("  │                                              │")
    print("  │  📊 API 请求日志将实时显示在下方              │")
    print("  │  按 Ctrl+C 停止 API 服务                     │")
    print("  └──────────────────────────────────────────────┘")
    print()
    print("══════════════ API 请求日志 ════════════════════")
    print()

    # 实时转发 uvicorn 输出到当前控制台
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
        proc.wait()
    except KeyboardInterrupt:
        print("\n正在停止 API 服务...")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("API 服务已停止")
        print("（爬虫窗口需要手动关闭）")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已停止")
    except Exception as e:
        print(f"\n启动失败: {e}")
        import traceback
        traceback.print_exc()
        input("按回车键退出...")
