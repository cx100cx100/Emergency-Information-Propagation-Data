# -*- coding: utf-8 -*-
"""知微事见 timePointV.do 重点账号记录采集工具。

仅在用户已获授权使用的网页与接口上运行。不会尝试绕过限制、验证码或权限。
默认单线程、每次查询间隔至少 3 秒；429/403/401 自动停止。
使用同源浏览器请求，并仅在本机浏览器内使用正常登录状态，不输出 token。
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "events.csv"
TIMELINES = ROOT / "timeline_csv"
OUTPUT = ROOT / "结果输出"
RAW_ROOT = OUTPUT / "raw"
CN_TZ = timezone(timedelta(hours=8))
BASE_URL = "https://ef.zhiweidata.com"
PLATFORMS = ("douyin", "weibo", "wechat", "media")
CHINESE = {"douyin": "抖音", "weibo": "微博", "wechat": "微信", "media": "网媒"}

# 此脚本在登录后，以与网站现有功能相同的请求方式访问同源接口。
# 请不要将 token 或浏览器配置目录发送给任何人。
JS_REQUEST = r"""async ({eventId, timePoint}) => {
  const body = new URLSearchParams({eventId, unit: 'hour', timePoint});
  const token = window.localStorage.getItem('token');
  const headers = {'Content-Type':'application/x-www-form-urlencoded;charset=UTF-8'};
  if (token) headers['token'] = token;
  try {
    const resp = await fetch('/analy/timePointV.do', {
      method:'POST', credentials:'same-origin', headers, body:body.toString()
    });
    const raw = await resp.text();
    let data;
    try { data = JSON.parse(raw); }
    catch(e) {return {ok:false, status:resp.status, error:'响应不是 JSON，可能是登录跳转或页面错误', sample:raw.slice(0,160)};}
    if (!resp.ok) return {ok:false, status:resp.status, error:data.message || 'HTTP 错误'};
    if (!data || data.state !== true || !['douyin','weibo','wechat','media'].every(p => Array.isArray(data[p]))) {
      return {ok:false, status:resp.status, error:(data && data.message) || '响应结构不符合预期/未获权限'};
    }
    return {ok:true, status:resp.status, data};
  } catch (e) {
    return {ok:false, status:-1, error:String(e)};
  }
}"""


def cli():
    p = argparse.ArgumentParser(description="按小时自动采集知微事见四渠道 TOP8 重点账号记录")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--event", help="事件名称关键字，例如：包钢")
    g.add_argument("--all", action="store_true", help="采集配置表中的全部14个事件（须先填好全部事件网址）")
    p.add_argument("--hours", type=int, default=24, help="从起始小时开始采集的连续小时数，默认24")
    p.add_argument("--peaks", type=int, default=0, help="额外采集本时段外的高峰小时数，默认0")
    p.add_argument("--interval", type=float, default=3.0, help="查询间隔秒数，不少于2秒，默认3秒")
    p.add_argument("--max-requests", type=int, default=0, help="本次最多新发起多少次请求（用于少量测试）")
    p.add_argument("--allow-partial", action="store_true", help="仅采集已填写网址的事件（即使其他事件尚未填写）")
    p.add_argument("--dry-run", action="store_true", help="只显示计划，不启动浏览器")
    p.add_argument("--rebuild", action="store_true", help="仅根据已保存的原始JSON重新生成CSV，不启动浏览器")
    args = p.parse_args()
    if args.hours < 1 or args.hours > 240:
        p.error("--hours 必须在1到240之间")
    if args.peaks < 0 or args.peaks > 100:
        p.error("--peaks 必须在0到100之间")
    if args.interval < 2:
        p.error("为减轻网站负担，--interval 不可小于2秒")
    if args.max_requests < 0:
        p.error("--max-requests 不能为负数")
    return args


def read_events():
    if not CONFIG.exists():
        raise RuntimeError("未找到 events.csv")
    with CONFIG.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    results = []
    for row in rows:
        name = (row.get("event_name") or "").strip()
        url = (row.get("event_url") or "").strip()
        start = (row.get("start_hour") or "").strip()
        if not name:
            continue
        eid = ""
        if url:
            parsed = urlparse(url)
            if parsed.netloc.lower() != "ef.zhiweidata.com" or parsed.scheme != "https":
                raise RuntimeError(f"{name} 的网址不是 https://ef.zhiweidata.com，请核对")
            m = re.search(r"/event(?:-sasac|Rk)?/([0-9a-fA-F]{24,64})(?:/|$)", parsed.path)
            if not m:
                raise RuntimeError(f"无法从 {name} 的网址提取 eventId，请填写完整事件页面链接")
            eid = m.group(1)
        if start:
            datetime.strptime(start, "%Y-%m-%d %H")

        # 容错：名称可能被 Excel/编码转换破坏；按日期前缀与原始 CSV 文件精确对应。
        # 仅当该日期恰好只有一个 CSV 文件时自动恢复；否则拒绝猜测。
        if not (TIMELINES / (name + ".csv")).exists():
            m_date = re.match(r"^(\d{4}-\d{2}-\d{2})-", name)
            if m_date:
                matches = sorted(TIMELINES.glob(m_date.group(1) + "-*.csv"))
                if len(matches) == 1:
                    recovered = matches[0].stem
                    print(f"提示：自动修正配置中的事件名：{name!r} -> {recovered!r}")
                    name = recovered
                elif len(matches) > 1:
                    raise RuntimeError(
                        f"事件 {name!r} 对应多个逐小时 CSV，无法自动判断；请检查 events.csv")
            if not (TIMELINES / (name + ".csv")).exists():
                raise RuntimeError(
                    f"未找到 {name!r} 对应的逐小时 CSV；请检查 timeline_csv 目录及 events.csv")
        results.append({"name":name, "url":url, "eventId":eid, "start":start})
    return results


def load_timeline(name):
    p = TIMELINES / (name + ".csv")
    if not p.exists():
        raise FileNotFoundError(f"缺少逐小时数据: {p.name}")
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        row["_dt"] = datetime.strptime(row["time"], "%Y-%m-%d %H")
        row["_count"] = int(row["all_num"])
    return rows


def select_hours(event, hours, peaks):
    rows = load_timeline(event["name"])
    nonzero = [r for r in rows if r["_count"] > 0]
    if not nonzero:
        return []
    start = (datetime.strptime(event["start"], "%Y-%m-%d %H")
             if event["start"] else nonzero[0]["_dt"])
    limit = start + timedelta(hours=hours)
    picked = {r["time"]:r for r in rows if start <= r["_dt"] < limit and r["_count"] > 0}
    if peaks:
        others = (r for r in rows if r["_count"] > 0 and r["time"] not in picked)
        for row in sorted(others, key=lambda r:(-r["_count"], r["_dt"]))[:peaks]:
            picked[row["time"]] = row
    return sorted(picked.values(), key=lambda r:r["_dt"])


def raw_file(eid, hour):
    return RAW_ROOT / eid / (hour.replace(" ", "_") + ".json")


def get_publish_time(value):
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, str):
        return value[:19]
    return str(value)


def flatten_and_report(selected):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    details, coverage = [], []
    for event in selected:
        if not event["eventId"]:
            continue
        eid = event["eventId"]
        row_lookup = {r["time"]:r for r in load_timeline(event["name"])}
        root = RAW_ROOT / eid
        for path in sorted(root.glob("*.json")):
            timepoint = path.stem.replace("_", " ")
            with path.open("r", encoding="utf-8") as f:
                res = json.load(f)
            stat = row_lookup.get(timepoint)
            for plat in PLATFORMS:
                accounts = res.get(plat, [])
                n = sum(len(a.get("infos", [])) for a in accounts)
                full_n = int(stat[f"{plat}_num"]) if stat else None
                full_accounts = int(stat[f"{plat}_sourceNum"]) if stat else None
                coverage.append({
                    "event_name":event["name"], "event_id":eid,"hour":timepoint,
                    "platform":CHINESE[plat], "platform_code":plat,
                    "timeline_full_posts":full_n if full_n is not None else "",
                    "timeline_full_accounts":full_accounts if full_accounts is not None else "",
                    "top8_post_count":n, "top8_account_count":len(accounts),
                    "post_sample_ratio":round(n / full_n, 4) if full_n else "",
                    "account_sample_ratio":round(len(accounts) / full_accounts, 4) if full_accounts else "",
                })
                for account in accounts:
                    for info in account.get("infos", []):
                        details.append({
                            "event_name":event["name"],"event_id":eid,"query_hour":timepoint,
                            "platform":CHINESE[plat],"platform_code":plat,
                            "account_name": account.get("name", ""),
                            "source":info.get("source", ""),
                            "account_id":info.get("userId", info.get("user_id", "")),
                            "record_id":info.get("id", ""),
                            "published_at_china":get_publish_time(info.get("time")),
                            "published_time_raw":info.get("time", ""),
                            "title":info.get("title", ""),
                            "content":info.get("content", info.get("text", "")),
                            "url":info.get("url", ""),
                            "h_raw":info.get("h", ""),
                            "like_num":info.get("likeNum", ""),
                            "comment_num":info.get("commentNum", ""),
                            "followers_num":info.get("followersNum", info.get("fensi", "")),
                            "create_time_raw":info.get("createTime", ""),
                        })
    # 避免重复记录；保留事件+平台+信息ID的唯一记录。
    uniq = {}
    for r in details:
        key = (r["event_id"],r["platform_code"],str(r["record_id"] or r["url"]))
        uniq.setdefault(key, r)
    details = sorted(uniq.values(), key=lambda r:(r["event_name"],r["published_at_china"],r["platform_code"]))
    if details:
        with (OUTPUT/"四渠道重点信息明细.csv").open("w",encoding="utf-8-sig",newline="") as f:
            w=csv.DictWriter(f,fieldnames=list(details[0])); w.writeheader(); w.writerows(details)
    if coverage:
        with (OUTPUT/"各小时样本覆盖率.csv").open("w",encoding="utf-8-sig",newline="") as f:
            w=csv.DictWriter(f,fieldnames=list(coverage[0])); w.writeheader(); w.writerows(coverage)
    print(f"\n已整理：{len(details)} 条唯一单条信息、{len(coverage)} 个小时-渠道组合。")
    print(f"输出目录：{OUTPUT}")
    if not details:
        print("提示：尚未采集到记录，未生成明细CSV。")


def launch_browser(p):
    # Windows 上的 Microsoft Edge。系统没有安装 Edge 时，再尝试 Google Chrome。
    for channel in ("msedge", "chrome"):
        try:
            ctx=p.chromium.launch_persistent_context(
                user_data_dir=str(ROOT/"_browser_profile"), channel=channel,
                headless=False, viewport={"width":1280,"height":850},
                accept_downloads=False)
            print(f"已启动 {channel}（独立浏览器配置，登录信息仅保存在本机）")
            return ctx
        except Exception as exc:
            print(f"无法启动 {channel}：{str(exc).splitlines()[0]}")
    raise RuntimeError("Edge/Chrome 启动失败。请确认电脑上已安装 Edge，或执行 python -m playwright install chromium 并修改脚本启动方式。")


def main():
    args=cli()
    events=read_events()
    if args.all:
        selected=events
        missing=[e["name"] for e in selected if not e["eventId"]]
        if missing and not args.allow_partial:
            print("以下事件未填写网址，不能以 --all 模式运行：")
            for name in missing: print("  -",name)
            print("填写 events.csv 的 event_url；或用 --allow-partial 仅处理已填写项目。")
            sys.exit(2)
    elif args.event:
        selected=[e for e in events if args.event in e["name"]]
        if len(selected)!=1:
            raise RuntimeError(f"关键字 {args.event!r} 匹配到 {len(selected)} 个事件，请改用更准确的名称")
    else:
        print("未指定 --all/--event，默认试采集包钢事件。")
        selected=[e for e in events if "包钢" in e["name"]]
    selected=[e for e in selected if e["eventId"]]
    if not selected: raise RuntimeError("没有可采集的事件，请先在 events.csv 填写 event_url。")
    if args.rebuild:
        flatten_and_report(selected)
        return
    todo=[]
    for event in selected:
        hh=select_hours(event,args.hours,args.peaks)
        missing=[r for r in hh if not raw_file(event["eventId"],r["time"]).exists()]
        todo += [(event,r) for r in missing]
        st=hh[0]["time"] if hh else "--"
        print(f"{event['name']} | 计划{len(hh)}小时，已缓存{len(hh)-len(missing)}，待请求{len(missing)}，起始{st}")
    print(f"\n全部待发请求：{len(todo)}（已跳过没有传播活动的小时及已保存数据）")
    if args.dry_run:
        print("预览模式结束。正式运行去掉 --dry-run。")
        return
    if not todo:
        print("所有目标小时已采集过，直接重新生成数据表。")
        flatten_and_report(selected)
        return
    if args.max_requests:
        todo=todo[:args.max_requests]
        print(f"测试模式：本次仅请求前{len(todo)}个小时")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("未安装 playwright：请在 Anaconda Prompt 执行 python -m pip install playwright")
    sent=0
    try:
        with sync_playwright() as p:
            ctx=launch_browser(p)
            try:
                page=ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(BASE_URL,wait_until="domcontentloaded",timeout=45000)
                print("\n请在新打开的 Edge/Chrome 窗口中正常登录知微事见。")
                print("如果已经登录，就无需重复登录。确认页面可访问后，回到终端按回车。")
                input("按回车开始采集（输入 Ctrl+C 可取消）：")
                if not page.url.startswith(BASE_URL):
                    page.goto(BASE_URL,wait_until="domcontentloaded",timeout=45000)
                for i,(ev,r) in enumerate(todo,1):
                    if not page.url.startswith(BASE_URL):
                        raise RuntimeError("浏览器已离开知微事见站点，请返回网站后重新执行")
                    hour=r["time"]
                    answer=page.evaluate(JS_REQUEST,{"eventId":ev["eventId"],"timePoint":hour})
                    if not answer.get("ok"):
                        status=answer.get("status")
                        print(f"\n请求失败：{ev['name']} {hour}；HTTP={status}；{answer.get('error','未知错误')}")
                        if status in (401,403,429): print("遇到权限/限流响应，已停止。请勿尝试绕过。")
                        else: print("检查登录状态、网络或数据可用性；已经保存的小时不会丢失。")
                        break
                    rec=answer["data"]
                    target=raw_file(ev["eventId"],hour)
                    target.parent.mkdir(parents=True,exist_ok=True)
                    target.write_text(json.dumps(rec,ensure_ascii=False,indent=2),encoding="utf-8")
                    counts=" / ".join(f"{CHINESE[k]}{sum(len(a.get('infos',[])) for a in rec[k])}" for k in PLATFORMS)
                    print(f"[{i}/{len(todo)}] {ev['name'][:16]} {hour}  {counts}")
                    sent+=1
                    if i<len(todo): time.sleep(args.interval+random.uniform(0,1.0))
            finally:
                ctx.close()
    except KeyboardInterrupt:
        print("\n用户停止采集；已保存数据保留，重新运行会自动断点续采。")
    finally:
        print(f"本次成功新采集 {sent} 个小时。")
        flatten_and_report(selected)


if __name__=='__main__':
    try: main()
    except Exception as err:
        print(f"\n错误：{err}",file=sys.stderr)
        sys.exit(1)
