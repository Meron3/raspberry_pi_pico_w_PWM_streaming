# 2025/12/19, サーバーがHTTPSしか対応していない(GitHub等)場合のコード
# Meron3, https://github.com/Meron3/raspberry_pi_pico_w_PWM_streaming
# 説明はこちら→https://zenn.dev/meron3/articles/fb4ca2c513ddb1

import network
import socket
import struct
import machine
import time
import gc
import _thread
import math
from machine import Pin
import ssl

led = Pin("LED", Pin.OUT)

# --- Wi-Fi接続設定 ---
SSID = "YOUR_WIFI_SSID"
PASSWORD = "YOUR_WIFI_PASSWORD"

# GitHubの場合。サーバーによって要変更。
WAV_HOST = "raw.githubusercontent.com"


# ---違うリポジトリから再生するときは、ここを書き換える---
# WAV_PATH = "/Meron3/raspberry_pi_pico_w_PWM_streaming/main/C_major.wav"
WAV_PATH = "/Meron3/raspberry_pi_pico_w_PWM_streaming/main/maou_14_shining_star.wav"
WAV_PORT = 443

PWM_PIN = 15
SENSOR_PIN = 26

# 動作設定
THRESHOLD_ON = 30.0
COOLDOWN_SEC = 5

# バッファサイズ。処理落ちする場合は値を小さくする。
BUF_SIZE = 10000

machine.freq(250000000)

# --- グローバル変数 ---
try:
    audio_buffer = bytearray(BUF_SIZE)
except MemoryError:
    print("Memory Alloc Failed at start.")
    raise

write_idx = 0
read_idx = 0
buf_count = 0
lock = _thread.allocate_lock()

req_play_start = False
flag_downloading = False
flag_eof = False

# --- 距離センサー ---
adc_sensor = machine.ADC(SENSOR_PIN)
R1 = 10000; R2 = 20000
VOLTAGE_DIVIDER_RATIO = (R1 + R2) / R2 

def read_distance():
    raw_value = adc_sensor.read_u16()
    pin_voltage = (raw_value / 65535) * 3.3
    sensor_voltage = pin_voltage * VOLTAGE_DIVIDER_RATIO
    if sensor_voltage <= 0.1: return 80.0 
    try:
        distance_cm = 29.988 * math.pow(sensor_voltage, -1.173)
    except:
        distance_cm = 0
    return distance_cm

# --- Download Thread ---
def download_thread():
    global write_idx, buf_count, flag_eof, flag_downloading, req_play_start
    
    print("[Thread] Connecting Wi-Fi...")
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(SSID, PASSWORD)
        while not wlan.isconnected():
            time.sleep(0.5)
    print("[Thread] Ready.")
    led.on()
    
    while True:
        while not req_play_start:
            time.sleep(0.05)
        
        print(f"[Thread] GET {WAV_HOST}...")
        flag_downloading = True
        flag_eof = False
        gc.collect() # 接続前にメモリ掃除
        
        s = None
        ss = None
        try:
            addr_info = socket.getaddrinfo(WAV_HOST, WAV_PORT)
            addr = addr_info[0][-1]
            
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10.0)
            s.connect(addr)
            
            # SSLラップ
            ss = ssl.wrap_socket(s, server_hostname=WAV_HOST)
            
            req = f"GET {WAV_PATH} HTTP/1.1\r\nHost: {WAV_HOST}\r\nUser-Agent: PicoW\r\nConnection: close\r\n\r\n"
            ss.write(req.encode())
            
            # ヘッダー読み飛ばし
            header_end = False
            last_4_bytes = b'\x00\x00\x00\x00'
            while not header_end:
                byte = ss.read(1)
                if not byte: break
                last_4_bytes = last_4_bytes[1:] + byte
                if last_4_bytes == b'\r\n\r\n':
                    header_end = True
            
            print("[Thread] Body Start")
            led.off()
            
            # 受信ループ
            temp_buf = bytearray(1024)
            while True:
                if not req_play_start: break
                try:
                    # SSL読み込み
                    data = ss.read(1024)
                    if not data: break
                    n = len(data)
                except OSError:
                    break
                
                # バッファ空き待ち
                while True:
                    if not req_play_start: break
                    with lock:
                        space = BUF_SIZE - buf_count
                    if space >= n:
                        break
                    time.sleep(0.005)
                
                if not req_play_start: break

                # 書き込み
                end_space = BUF_SIZE - write_idx
                if n <= end_space:
                    audio_buffer[write_idx : write_idx + n] = data
                    write_idx = (write_idx + n) % BUF_SIZE
                else:
                    audio_buffer[write_idx : BUF_SIZE] = data[:end_space]
                    remain = n - end_space
                    audio_buffer[0 : remain] = data[end_space:]
                    write_idx = remain
                
                with lock:
                    buf_count += n
            
        except Exception as e:
            print("[Thread] Error:", e)
        finally:
            if ss: 
                ss.close()
                ss = None
            if s: 
                s.close()
                s = None
            gc.collect() 
        
        print("[Thread] Done")
        led.on()
        flag_eof = True
        flag_downloading = False
        
        while req_play_start:
            time.sleep(0.1)

# --- Audio Player ---
class AudioPlayer:
    def __init__(self, pin_num):
        self.pwm = machine.PWM(machine.Pin(pin_num))
        self.pwm.freq(125000)
        self.pwm.duty_u16(0)
        # Timerのインスタンスだけ先に作っておく
        self.timer = machine.Timer()
        
    @micropython.native
    def play_tick(self, t):
        global read_idx, buf_count
        if buf_count >= 2:
            buf = audio_buffer
            ridx = read_idx
            low = buf[ridx]
            ridx = (ridx + 1) % BUF_SIZE
            high = buf[ridx]
            ridx = (ridx + 1) % BUF_SIZE
            read_idx = ridx
            with lock:
                buf_count -= 2
            val = (high << 8) | low
            self.pwm.duty_u16(val ^ 0x8000)
        else:
            self.pwm.duty_u16(32768)

    def start(self, sample_rate):
        gc.collect()
        try:
            # メモリ断片化で失敗する場合の最終手段：再試行ロジック
            self.timer.init(freq=sample_rate, mode=machine.Timer.PERIODIC, callback=self.play_tick)
        except OSError as e:
            print(f"Timer Init Failed: {e}")
            # Timer作成に失敗してもプログラムを落とさない

    def stop(self):
        self.timer.deinit()
        self.pwm.duty_u16(0)

def play_one_shot(player):
    global req_play_start, write_idx, read_idx, buf_count
    
    print(">>> Triggered!")
    with lock:
        write_idx = 0
        read_idx = 0
        buf_count = 0
    
    gc.collect()
    req_play_start = True
    
    # ★修正2: バッファサイズ縮小に伴い、待機閾値を4000(0.5秒分)に下げる
    timeout = 0
    while buf_count < 4000 and not flag_eof:
        time.sleep(0.1)
        timeout += 1
        if timeout > 150:
            print("Timeout")
            req_play_start = False
            return

    if flag_eof and buf_count < 100:
        print("Download Failed (No Data)")
        req_play_start = False
        return

    player.start(8000)
    
    try:
        while True:
            if flag_eof and buf_count == 0: break
            if not flag_downloading and buf_count == 0: break
            
            if buf_count == 0 and flag_downloading:
                time.sleep(0.01)
            else:
                time.sleep(0.5)
    finally:
        player.stop()
        req_play_start = False
        print(">>> Finished.")

def main():
    gc.threshold(50000)
    _thread.start_new_thread(download_thread, ())
    player = AudioPlayer(PWM_PIN)
    
    print("--- System Ready (Ultra LowMem) ---")
    try:
        while True:
            dist = read_distance()
            if 0 < dist < THRESHOLD_ON:
                play_one_shot(player)
                print(f"Cooldown {COOLDOWN_SEC}s...")
                time.sleep(COOLDOWN_SEC)
                gc.collect()
                print("Ready.")
            time.sleep(0.2)
    except KeyboardInterrupt:
        player.stop()
        req_play_start = False

if __name__ == "__main__":
    main()
