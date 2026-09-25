import asyncio
import websockets
import base64
import time
import json
import requests
from aiortc.sdp import candidate_from_sdp, candidate_to_sdp
from aiortc.contrib.signaling import object_from_string, object_to_string
from aiortc import RTCIceCandidate, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.signaling import BYE, add_signaling_arguments, create_signaling
from aiortc.contrib.media import MediaBlackhole, MediaPlayer, MediaRecorder, MediaRelay

relay = MediaRelay()
sub_key = None

def login(phonenumber):
    req_smscode = requests.post('https://n.cg.163.com/api/v1/phone-captchas/' + phonenumber)
    print("input the code recieved by your phone")
    code = input().strip()
    headers = {"Content-Type": "application/json;charset=utf-8"}
    data = {
        "auth_method":"phone-captcha",
        "ctcode": phonenumber.split("-")[0],
        "phone": phonenumber.split("-")[1],
        "captcha": code,
        "device_info":{
            "userAgent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:91.0) Gecko/20100101 Firefox/91.0",
            "appVersion":"5.0 (Macintosh)",
            "codecs":["h264","vp8","vp9"]
            }
        }
    data = json.dumps(data)
    user_info = requests.post("https://n.cg.163.com/api/v1/tokens", headers=headers, data=data).text
    user_obj = json.loads(user_info)
    try:
        token = user_obj["token"]
        with open("token","w") as f:
            f.write(token)
        print("login ok!")
    except:
        print("login error! try again")
        exit(-1)
    return 


def encode_mess(message):
    global sub_key
    mess = str.encode(message)
    m = b""
    for i in mess:
        m += ((i+sub_key)%256).to_bytes(1, byteorder='big')
    res = base64.b64encode(m)
    return res

def decode_mess(message):
    try:
        mess = base64.b64decode(message)
    except:
        return message
    global sub_key
    if sub_key == None:
        fbyte = mess[0]
        sbyte = mess[1]
        for j in range(0, 256):
            if(chr((fbyte-j)%256)=="[" and chr((sbyte-j)%256) == "{" and chr((mess[2]-j)%256) == "\""):
                sub_key = j
                break
            if chr((fbyte-j)%256) == "{" and chr((sbyte-j)%256) == "\"":
                sub_key = j
                break
    m = ""
    for i in mess:
         m += chr((i-sub_key)%256)
    return m

def get_basic_info(token):
    headers = {
        "Authorization": "Bearer " +token
    }
    info = requests.get("https://n.cg.163.com/api/v2/users/@me", headers=headers).text
    info = json.loads(decode_mess(info))
    return info["yunxin_account"]["accid"]

def request_ticket(token, game_code, regions=["hdcz","hbsjz"], codecs = ["h264","vp8","vp9"], width = 1280, height = 720):
    req_obj = {
        "regions": regions,
        "game_code": game_code,
        "codecs": codecs,
        "width": width,
        "height": height
    }
    global sub_key
    
    req_str = encode_mess(json.dumps(req_obj).replace("'","\""))
    headers = {
        "Authorization": "Bearer " +token,
        "Content-Type": "application/octet-stream" 
    }
    info = requests.post("https://n.cg.163.com/api/v2/tickets", headers=headers, data= req_str).text
    info_obj = json.loads(decode_mess(info))
    print(info_obj)
    return info_obj["gateway_url"]

def find_region(token, game_code):
    headers = {
        "Authorization": "Bearer " +token
    }
    resp_text = decode_mess(requests.get("https://n.cg.163.com/api/v2/media-servers?game_code="+game_code, headers=headers).text)
    r = json.loads(resp_text)
    # 服务端响应结构可能为:
    #   1) 旧版: 直接是 region 数组
    #   2) 新版: {"regions": [...], ...} 或 {"data": [...]} 包裹
    # 兼容处理, 避免 "string indices must be integers" 崩溃。
    if isinstance(r, dict):
        for key in ("regions", "data", "region"):
            if isinstance(r.get(key), list):
                r = r[key]
                break
        else:
            # 兜底: 字符串 region 字段
            region = r.get("region")
            if isinstance(region, str):
                return [region]
            raise ValueError(f"无法解析 media-servers 响应: {str(r)[:200]}")
    if not isinstance(r, list):
        raise ValueError(f"media-servers 响应类型异常: {type(r).__name__}: {str(r)[:200]}")

    regions = []
    for obj in r:
        if isinstance(obj, dict) and "region" in obj:
            regions.append(obj["region"])
        elif isinstance(obj, str):
            regions.append(obj)
    if not regions:
        print(f"[warn] find_region 未获取到 region, 原始响应: {str(r)[:300]}")
    return regions

def exit_game(token, game_code):
    headers = {
        "Authorization": "Bearer " +token
    }
    # 注意: URL 需格式化 game_code(原代码遗漏了 {} 替换)
    url = f"https://n.cg.163.com/api/v2/users/@me/games-playing/{game_code}"
    try:
        print(requests.delete(url, headers=headers).text)
    except Exception as e:
        # 退出游戏失败不应阻断主流程(网络抖动/额度耗尽等)
        print(f"[warn] exit_game 失败(忽略): {e}")

async def connect(token, game_code, w=1280, h=720, quality="high", codecs=["h264","vp8","vp9"], platform=0, fps="30"):
        user_id = get_basic_info(token)
        regions = find_region(token, game_code)
        uri = request_ticket(token, game_code, regions=regions)
        websocket = await websockets.connect(uri)
        auth_obj = { 
            "id": str(int(round(time.time() * 1000))),
            "op": "auth",
            "data": {
                "user_id": user_id,
                "token": token,
                "game_code": game_code,
                "w": w,
                "h": h,
                "quality": quality,
                "codecs": codecs,
                "platform": platform,
                "fps": fps
            }
        }
        auth_str = json.dumps(auth_obj)
        await websocket.send(auth_str)
        auth_res = await websocket.recv()
        auth_res = decode_mess(auth_res)
        try:
            de_res = json.loads(auth_res)
        except:
            print("[x] something went wrong when decoding auth response")
            exit(-1)
        if de_res["op"] != "offer":
            print("[x] "+ de_res["data"]["errmsg"])
            exit(-1)
        new_sdp = {}
        new_sdp["type"] = de_res["op"]
        new_sdp["sdp"] = de_res["data"]["sdp"]
        new_sdp = json.dumps(new_sdp)
        return new_sdp, websocket


def channel_log(channel, t, message):
    print("channel(%s) %s %s" % (channel.label, t, message))

async def send_action(sock, action, wait_pong=True, timeout=5.0):
    """发送一条控制命令到云游戏 WebSocket。

    原实现每条命令后都强制 ping 并等待 PONG, 高并发命令(如滑动分解出的
    几十个触摸点)在弱网下会逐一积压, 造成「命令卡住」。
    因此:
    - wait_pong=False: 只发送不等待(滑动逐点使用, 显著提速);
    - wait_pong=True:  发送后等待 PONG, 但带超时——超时不抛异常,
      命令已到达服务端, 不能因为 ping 失败阻塞整条链路。
    """
    action = encode_mess(json.dumps(action).replace("'", "\""))
    await sock.send(action)
    if not wait_pong:
        return
    try:
        pong_waiter = await asyncio.wait_for(sock.ping(), timeout=timeout)
        await asyncio.wait_for(pong_waiter, timeout=timeout)
    except (asyncio.TimeoutError, Exception):
        pass  # ping 超时仅告警级别, 不阻断业务流

def pack_message(cmd, data):
    if cmd == "mm": # move mouse, data: x, y
        action = {"id":str(int(round(time.time() * 1000))),"op":"input","data":{"cmd":"1 %d %d 0" % (data["x"], data["y"])}}
    elif cmd == "cm": # click mouse, data: x, y
        action = {"id":str(int(round(time.time() * 1000))),"op":"input","data":{"cmd":"3 %d %d 0" % (data["x"], data["y"])}}
    elif cmd == "ip": # keyboard input, data: single keyborad word
        action = {"id":str(int(round(time.time() * 1000))),"op":"input","data":{"cmd":"5 %s" % data["word"]}}
    else:
        action = {}
    return action

#print(decode_mess("2YDHwoCYgI+VlI6SlpeTk4+Uj5eAioDNzoCYgMfMztPSgIqAwr/Sv4CY2YDBy8KAmICRfpaOln6TkZB+joDb2w=="))