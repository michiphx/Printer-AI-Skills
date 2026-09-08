#!/usr/bin/env python3
"""
跨平台本地打印机 CLI

提供 CLI 命令来操作本地打印机，支持 Windows、macOS、Linux。
配合 AI Skill 系统（OpenClaw / Cursor / Claude 等）使用。

用法:
    printer-ai printers              # 列出打印机
    printer-ai status [INDEX]        # 获取打印机状态
    printer-ai attrs [INDEX]         # 获取打印机属性
    printer-ai print FILE            # 打印文件
    printer-ai jobs                  # 列出打印任务
    printer-ai job-status JOB_ID     # 查询任务状态
    printer-ai cancel-job JOB_ID     # 取消打印任务
"""

import argparse
import json
import sys
from sys import platform

# 根据系统平台导入对应的打印机模块
if platform == "win32":
    from local_printer.windows import (
        get_printer_list as _get_printer_list,
        get_printer_status as _get_printer_status,
        get_printer_attrs as _get_printer_attrs,
        print_file as _print_file,
        get_print_jobs as _get_print_jobs,
        get_print_job_status as _get_print_job_status,
        cancel_print_job as _cancel_print_job,
    )
    from models.model import WindowsPrintOptions as PrintOptionsClass
elif platform == "linux" or platform == "darwin":
    from local_printer.cups import (
        get_printer_list as _get_printer_list,
        get_printer_status as _get_printer_status,
        get_printer_attrs as _get_printer_attrs,
        print_file as _print_file,
        get_print_jobs as _get_print_jobs,
        get_print_job_status as _get_print_job_status,
        cancel_print_job as _cancel_print_job,
    )
    from models.model import LinuxPrintOptions as PrintOptionsClass
else:
    _get_printer_list = None
    _get_printer_status = None
    _get_printer_attrs = None
    _print_file = None
    _get_print_jobs = None
    _get_print_job_status = None
    _cancel_print_job = None
    PrintOptionsClass = None


def output_json(data):
    """输出 JSON（便于 AI 解析）"""
    print(json.dumps(data, indent=2, ensure_ascii=False))


def check_platform():
    """检查平台是否支持"""
    if platform not in ("win32", "linux", "darwin"):
        print(f"❌ 不支持的操作系统: {platform}", file=sys.stderr)
        sys.exit(1)


# ==================== 子命令实现 ====================


def cmd_printers(args):
    """列出打印机"""
    check_platform()
    result = _get_printer_list()

    if args.json:
        output_json(result)
        return

    if result.get("code") != 200:
        print(f"❌ 获取打印机列表失败: {result.get('msg', '')}", file=sys.stderr)
        sys.exit(1)

    printers = result.get("data", {}).get("printers", [])
    default_printer = result.get("data", {}).get("default_printer", "")

    if not printers:
        print("未找到打印机")
        return

    print(f"找到 {len(printers)} 台打印机:\n")
    for p in printers:
        default_mark = " ⭐默认" if p.get("is_default") else ""
        status = p.get("status", "unknown")
        status_icon = {"idle": "🟢", "processing": "🟡", "stopped": "🔴"}.get(status, "⚪")
        print(f"  [{p.get('index')}] {p.get('name', '未知')}{default_mark}")
        print(f"      状态: {status_icon} {status}  |  型号: {p.get('model', '未知')}")
        if p.get("location"):
            print(f"      位置: {p.get('location')}")


def cmd_status(args):
    """获取打印机状态"""
    check_platform()
    result = _get_printer_status(args.index)

    if args.json:
        output_json(result)
        return

    if result.get("code") != 200:
        print(f"❌ 获取打印机状态失败: {result.get('msg', '')}", file=sys.stderr)
        sys.exit(1)

    data = result.get("data", {})
    status = data.get("status", "unknown")
    status_icon = {"idle": "🟢", "processing": "🟡", "stopped": "🔴"}.get(status, "⚪")
    print(f"打印机: {data.get('name', '未知')}")
    print(f"状态: {status_icon} {status}")
    print(f"接受任务: {'✅ 是' if data.get('is_accepting_jobs') else '❌ 否'}")


def cmd_attrs(args):
    """获取打印机属性"""
    check_platform()
    result = _get_printer_attrs(args.index)
    output_json(result)


def cmd_print(args):
    """打印文件"""
    check_platform()
    import os

    if not os.path.exists(args.file_path):
        print(f"❌ 文件不存在: {args.file_path}", file=sys.stderr)
        sys.exit(1)

    # 解析打印选项
    print_options = None
    if args.options and PrintOptionsClass:
        try:
            options_dict = json.loads(args.options)
            print_options = PrintOptionsClass.from_dict(options_dict)
        except json.JSONDecodeError:
            print("❌ 打印选项 JSON 格式错误", file=sys.stderr)
            sys.exit(1)

    result = _print_file(args.index, args.file_path, print_options)

    if result.get("code") == 200:
        data = result.get("data", {})
        job_id = data.get("job_id", "")
        print(f"✅ 打印任务已提交  job_id: {job_id}")
        print(f"   打印机: {data.get('printer_name', '')}")
        print(f"   文件: {data.get('file_path', '')}")
        print(f"   查询状态: printer-ai job-status {job_id}")
    else:
        print(f"❌ 打印失败: {result.get('msg', '')}", file=sys.stderr)
        output_json(result)
        sys.exit(1)


def cmd_jobs(args):
    """列出打印任务"""
    check_platform()
    result = _get_print_jobs(args.printer)

    if args.json:
        output_json(result)
        return

    if result.get("code") != 200:
        print(f"❌ 获取打印任务失败: {result.get('msg', '')}", file=sys.stderr)
        sys.exit(1)

    jobs = result.get("data", {}).get("jobs", [])
    if not jobs:
        print("没有打印任务")
        return

    print(f"共 {len(jobs)} 个打印任务:\n")
    for job in jobs:
        status = job.get("status", "unknown")
        status_icon = {
            "pending": "⏳", "processing": "🔄", "completed": "✅",
            "canceled": "🚫", "aborted": "❌"
        }.get(status, "⚪")
        print(f"  [{job.get('job_id')}] {job.get('job_name', '未知')}  {status_icon} {status}")
        print(f"      打印机: {job.get('printer_name', '')}")


def cmd_job_status(args):
    """查询打印任务状态（同时返回打印机状态）"""
    check_platform()
    result = _get_print_job_status(args.job_id)

    # 获取打印机状态并合并到结果中
    printer_name = result.get("data", {}).get("printer_name", "")
    if printer_name and result.get("code") == 200:
        # 从打印机列表中找到对应打印机的 index
        printer_list_result = _get_printer_list()
        if printer_list_result.get("code") == 200:
            for p in printer_list_result["data"].get("printers", []):
                if p.get("name") == printer_name:
                    status_result = _get_printer_status(p["index"])
                    if status_result.get("code") == 200:
                        result["data"]["printer_status"] = status_result["data"]
                    break

    output_json(result)


def cmd_cancel_job(args):
    """取消打印任务"""
    check_platform()
    result = _cancel_print_job(args.job_id)
    output_json(result)
    if result.get("code") == 200:
        print("✅ 打印任务已取消")


# ==================== 网络发现 / 安装命令 ====================


def _fail(result):
    """Print the error of a failed APIResponse and exit."""
    print(f"❌ {result.get('msg', 'failed')}", file=sys.stderr)
    sys.exit(1)


def cmd_discover(args):
    """扫描局域网中的打印机 - scan the LAN for printers"""
    from local_printer import commands_net

    result = commands_net.discover(subnet=args.subnet, timeout=args.timeout, deep=not args.fast)
    if args.json:
        output_json(result)
        return
    if result.get("code") != 200:
        _fail(result)

    data = result["data"]
    printers = data["printers"]
    print(f"扫描 {data['subnet']} - 找到 {data['count']} 台打印机\n")
    for entry in printers:
        ipp = entry.get("ipp") or {}
        model = ipp.get("make_and_model") or entry.get("vendor_hint") or "unbekanntes Modell"
        state = ipp.get("state")
        icon = {"idle": "🟢", "processing": "🟡", "stopped": "🔴"}.get(state, "⚪")
        print(f"  {entry['host']}  {icon} {model}")
        print(f"      Ports: {', '.join(entry['open_ports'])}")
        if entry.get("mac"):
            print(f"      MAC:   {entry['mac']}")
        if ipp.get("supports_duplex") is not None:
            duplex = "ja" if ipp["supports_duplex"] else "nein"
            print(f"      Duplex: {duplex}  |  Standardmedium: {ipp.get('media_default', '?')}")
    if not printers:
        print("  (keine gefunden - ggf. --subnet angeben)")


def cmd_probe(args):
    """探测单个主机 - probe one host"""
    from local_printer import commands_net

    result = commands_net.probe(args.host, timeout=args.timeout)
    output_json(result)


def cmd_diagnose(args):
    """核对已安装打印机是否真的在线 - verify installed printers against the network"""
    from local_printer import commands_net

    result = commands_net.diagnose(deep=not args.fast, timeout=args.timeout)
    if args.json:
        output_json(result)
        return
    if result.get("code") != 200:
        _fail(result)

    data = result["data"]
    print(f"{data['count']} 台打印机: {data['online']} 在线, {data['offline']} 离线\n")
    for entry in data["printers"]:
        icon = "🟢" if entry["really_online"] else "🔴"
        default = " ⭐" if entry.get("is_default") else ""
        print(f"  [{entry['index']}] {entry['name']}{default}")
        print(f"      Spooler: {entry['spooler_status']}  |  {icon} {entry['verdict']}")
        if entry.get("model"):
            print(f"      Geraet:  {entry['model']} ({entry.get('device_state', '?')})")


def cmd_ports(args):
    from local_printer import commands_net

    output_json(commands_net.ports())


def cmd_drivers(args):
    from local_printer import commands_net

    output_json(commands_net.drivers(model=args.model))


def cmd_setup(args):
    """安装网络打印机（优先完整驱动） - install a network printer, best driver first"""
    from local_printer import commands_net

    result = commands_net.setup(
        args.host, name=args.name, dry_run=args.dry_run, allow_generic=not args.no_generic
    )
    if args.json or args.dry_run:
        output_json(result)
        return
    if result.get("code") != 200:
        output_json(result)
        _fail(result)

    data = result["data"]
    strategy = data.get("installed_with") or {}
    print(f"✅ Drucker eingerichtet: {data.get('printer')}")
    print(f"   Methode: {strategy.get('kind')}  |  Treiber: {strategy.get('driver')}")
    print(f"   Port:    {strategy.get('port')}")

    verification = data.get("verification") or {}
    if verification.get("full_featured"):
        print("   ✅ Alle Geraetefunktionen verfuegbar")
    else:
        print("   ⚠️  Eingeschraenkte Funktionen:")
        for item in verification.get("missing", []):
            print(f"      - {item}")
    if data.get("hint"):
        print(f"   💡 {data['hint']}")


def cmd_driver_search(args):
    """在厂商网站上查找驱动 - look up a manufacturer driver online"""
    from local_printer import commands_net

    result = commands_net.driver_search(
        model=args.model, host=args.host, region=args.region,
        os_code=args.os, download_dir=args.download,
    )
    if args.json:
        output_json(result)
        return
    if result.get("code") != 200:
        _fail(result)

    data = result["data"]
    print(f"Modell:  {data['model']}")
    print(f"Hersteller: {data['vendor']}  |  Region: {data.get('region')}  |  "
          f"OS: {data['os']['release']} {data['os']['arch']}")

    if not data.get("supported"):
        print(f"\n⚠️  {data.get('hint', '')}")
        if data.get("support_site"):
            print(f"   Support-Seite (ungeprueft): {data['support_site']}")
        return

    downloads = data.get("downloads") or []
    if downloads:
        print(f"\n✅ {len(downloads)} Treiberpaket(e) gefunden:\n")
        for item in downloads:
            size = f"{item['size_mb']} MB" if item.get("size_mb") else "?"
            print(f"  [{item['category']}] v{item['version']}  ({size})")
            print(f"      {item['filename']}")
            print(f"      {item['url']}")
    else:
        print(f"\n⚠️  {data.get('note', 'Keine direkten Download-Links verfuegbar.')}")
    print(f"\n🔗 Download-Seite: {data['download_page']}")

    dl = data.get("download")
    if dl:
        if dl.get("ok"):
            print(f"\n⬇️  Gespeichert: {dl['path']}")
            print(f"   {dl['note']}")
        else:
            print(f"\n❌ Download fehlgeschlagen: {dl.get('error')}")


def cmd_remove(args):
    from local_printer import commands_net

    if not args.yes:
        print("❌ Refusing to remove a printer without --yes", file=sys.stderr)
        sys.exit(1)
    output_json(commands_net.remove(args.name))


def cmd_set_default(args):
    from local_printer import commands_net

    output_json(commands_net.set_default(args.name))


# ==================== 主入口 ====================


def main():
    parser = argparse.ArgumentParser(
        prog="printer-ai",
        description="跨平台本地打印机 CLI - 让 AI 驱动本地打印",
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # printers
    p_printers = subparsers.add_parser("printers", help="列出打印机")
    p_printers.add_argument("--json", action="store_true", help="JSON 格式输出")
    p_printers.set_defaults(func=cmd_printers)

    # status
    p_status = subparsers.add_parser("status", help="获取打印机状态")
    p_status.add_argument("index", type=int, nargs="?", default=None,
                          help="打印机索引 (从1开始，默认: 默认打印机)")
    p_status.add_argument("--json", action="store_true", help="JSON 格式输出")
    p_status.set_defaults(func=cmd_status)

    # attrs
    p_attrs = subparsers.add_parser("attrs", help="获取打印机属性")
    p_attrs.add_argument("index", type=int, nargs="?", default=None,
                         help="打印机索引 (从1开始)")
    p_attrs.set_defaults(func=cmd_attrs)

    # print
    p_print = subparsers.add_parser("print", help="打印文件")
    p_print.add_argument("file_path", help="要打印的文件路径")
    p_print.add_argument("--index", type=int, default=None,
                         help="打印机索引 (从1开始，默认: 默认打印机)")
    p_print.add_argument("--options", help="打印选项 (JSON 格式字符串)")
    p_print.set_defaults(func=cmd_print)

    # jobs
    p_jobs = subparsers.add_parser("jobs", help="列出打印任务")
    p_jobs.add_argument("--printer", default=None, help="筛选指定打印机名称")
    p_jobs.add_argument("--json", action="store_true", help="JSON 格式输出")
    p_jobs.set_defaults(func=cmd_jobs)

    # job-status
    p_js = subparsers.add_parser("job-status", help="查询打印任务状态")
    p_js.add_argument("job_id", type=int, help="任务 ID")
    p_js.set_defaults(func=cmd_job_status)

    # cancel-job
    p_cj = subparsers.add_parser("cancel-job", help="取消打印任务")
    p_cj.add_argument("job_id", type=int, help="任务 ID")
    p_cj.set_defaults(func=cmd_cancel_job)

    # discover
    p_disc = subparsers.add_parser("discover", help="扫描局域网中的打印机")
    p_disc.add_argument("--subnet", default=None, help="要扫描的 /24 网段，如 192.168.1")
    p_disc.add_argument("--timeout", type=float, default=0.6, help="每个端口的超时秒数")
    p_disc.add_argument("--fast", action="store_true", help="跳过 IPP 身份查询")
    p_disc.add_argument("--json", action="store_true", help="JSON 格式输出")
    p_disc.set_defaults(func=cmd_discover)

    # probe
    p_probe = subparsers.add_parser("probe", help="探测单个主机是否为打印机")
    p_probe.add_argument("host", help="IP 地址")
    p_probe.add_argument("--timeout", type=float, default=1.0, help="超时秒数")
    p_probe.set_defaults(func=cmd_probe)

    # diagnose
    p_diag = subparsers.add_parser("diagnose", help="核对已安装打印机是否真的在线")
    p_diag.add_argument("--timeout", type=float, default=1.0, help="超时秒数")
    p_diag.add_argument("--fast", action="store_true", help="跳过 IPP 身份查询")
    p_diag.add_argument("--json", action="store_true", help="JSON 格式输出")
    p_diag.set_defaults(func=cmd_diagnose)

    # ports
    p_ports = subparsers.add_parser("ports", help="列出打印机端口")
    p_ports.set_defaults(func=cmd_ports)

    # drivers
    p_drv = subparsers.add_parser("drivers", help="列出打印机驱动")
    p_drv.add_argument("--model", default=None, help="按型号匹配候选驱动")
    p_drv.set_defaults(func=cmd_drivers)

    # setup
    p_setup = subparsers.add_parser("setup", help="安装网络打印机（优先完整驱动）")
    p_setup.add_argument("host", help="打印机 IP 地址")
    p_setup.add_argument("--name", default=None, help="队列名称（默认使用设备型号）")
    p_setup.add_argument("--dry-run", action="store_true", help="只显示计划，不做更改")
    p_setup.add_argument("--no-generic", action="store_true",
                         help="拒绝通用驱动回退（宁可失败也不降级）")
    p_setup.add_argument("--json", action="store_true", help="JSON 格式输出")
    p_setup.set_defaults(func=cmd_setup)

    # driver-search
    p_ds = subparsers.add_parser("driver-search", help="在厂商网站上查找驱动")
    p_ds.add_argument("model", nargs="?", default=None, help="打印机型号，如 'EPSON ET-4850 Series'")
    p_ds.add_argument("--host", default=None, help="改为通过 IPP 从该 IP 读取型号")
    p_ds.add_argument("--region", default=None, help="两位区域代码，如 DE / US（默认: 系统区域）")
    p_ds.add_argument("--os", default=None, help="厂商 OS 代码（默认: 自动检测）")
    p_ds.add_argument("--download", default=None, metavar="DIR",
                      help="下载安装包到该目录（仅下载，不执行）")
    p_ds.add_argument("--json", action="store_true", help="JSON 格式输出")
    p_ds.set_defaults(func=cmd_driver_search)

    # remove
    p_rm = subparsers.add_parser("remove", help="删除打印机队列")
    p_rm.add_argument("name", help="打印机名称")
    p_rm.add_argument("--yes", action="store_true", help="确认删除")
    p_rm.set_defaults(func=cmd_remove)

    # set-default
    p_sd = subparsers.add_parser("set-default", help="设置默认打印机")
    p_sd.add_argument("name", help="打印机名称")
    p_sd.set_defaults(func=cmd_set_default)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    try:
        args.func(args)
    except Exception as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
