# 2025/12/19, サーバーがHTTPストリーミングに対応している(さくらのレンタルサーバ等)場合、ローカルネットワークの他のPCから再生する場合のコード
# Meron3, https://github.com/Meron3/raspberry_pi_pico_w_PWM_streaming
# 説明はこちら→https://zenn.dev/meron3/articles/fb4ca2c513ddb1

import network
import urequests
import struct
import machine
import time
import gc
import _thread
import math
from machine import Pin

led = Pin("LED", Pin.OUT)

# --- Wi-Fi接続設定 ---
SSID = "YOUR_WIFI_SSID"
PASSWORD = "YOUR_WIFI_PASSWORD"

# ここをサーバー上にある音源ファイルの直リンクに書き換える
WAV_URL = "https://example.ne.jp/test.wav"

PWM_PIN = 15
SENSOR_PIN = 26

# 距離設定 (cm)
THRESHOLD_TRIGGER = 30.0  # この距離より近づいたら再生開始
THRESHOLD_RESET = 40.0    # この距離より離れるまで次は再生しない

# バッファ設定
BUF_SIZE = 40000
machine.freq(250000000)

# --- グローバル変数 ---
audio_buffer = bytearray(BUF_SIZE)
write_idx = 0
read_idx = 0
buf_count = 0
lock = _thread.allocate_lock()

# 状態管理フラグ
download_finished = False # ダウンロード完了フラグ
playback_finished = False # 再生完了フラグ

# --- 距離センサー (変更なし) ---
adc_sensor = machine.ADC(SENSOR_PIN)
R1 = 10000
R2 = 20000
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

def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print("Connecting Wi-Fi...")
        wlan.connect(SSID, PASSWORD)
        while not wlan.isconnected():
            time.sleep(0.5)
    print("Wi-Fi Ready")
    led.on()

# --- Core 1: ダウンロードタスク ---
# 呼ばれるたびに1回だけダウンロードして終了する
def download_task():
    global write_idx, buf_count, download_finished
    
    try:
        print("[Thread] Requesting WAV...")
        res = urequests.get(WAV_URL, stream=True)
        temp_buf = bytearray(2048)
        
        # ヘッダ読み飛ばし (簡易)
        # 必要ならここでヘッダ解析を入れるが、今回はデータとして流し込む
        
        while True:
            n = res.raw.readinto(temp_buf)
            if not n: 
                break # ダウンロード完了
            
            # バッファ空き待ち
            while True:
                with lock:
                    space = BUF_SIZE - buf_count
                if space >= n:
                    break
                time.sleep(0.005) # 少し待つ
            
            # リングバッファへ書き込み
            end_space = BUF_SIZE - write_idx
            if n <= end_space:
                audio_buffer[write_idx : write_idx + n] = temp_buf[:n]
                write_idx = (write_idx + n) % BUF_SIZE
            else:
                audio_buffer[write_idx : BUF_SIZE] = temp_buf[:end_space]
                remain = n - end_space
                audio_buffer[0 : remain] = temp_buf[end_space:n]
                write_idx = remain
            
            with lock:
                buf_count += n
        
        res.close()
        
    except Exception as e:
        print("[Thread] Error:", e)
    finally:
        download_finished = True
        print("[Thread] Download Complete")

# --- Core 0: 音声再生クラス ---
class AudioPlayer:
    def __init__(self, pin_num):
        self.pwm = machine.PWM(machine.Pin(pin_num))
        self.pwm.freq(125000)
        self.pwm.duty_u16(32768)
        self.timer = machine.Timer()
        
    @micropython.native
    def play_tick(self, t):
        global read_idx, buf_count, playback_finished
        
        if buf_count >= 2:
            buf = audio_buffer
            ridx = read_idx
            
            low = buf[ridx]
            ridx += 1
            if ridx >= BUF_SIZE: ridx = 0
            high = buf[ridx]
            ridx += 1
            if ridx >= BUF_SIZE: ridx = 0
            
            read_idx = ridx
            
            with lock:
                buf_count -= 2
            
            val = (high << 8) | low
            self.pwm.duty_u16(val ^ 0x8000)
            
        else:
            # バッファが空の場合
            self.pwm.duty_u16(32768) # 無音
            
            # ダウンロードも終わっていて、バッファも空なら再生終了
            if download_finished:
                playback_finished = True

    def start(self, sample_rate):
        self.timer.init(freq=sample_rate, mode=machine.Timer.PERIODIC, callback=self.play_tick)

    def stop(self):
        self.timer.deinit()
        self.pwm.duty_u16(0)

# --- 1回再生する関数 ---
def play_one_shot(player):
    global write_idx, read_idx, buf_count, download_finished, playback_finished
    
    # 1. 変数リセット
    write_idx = 0
    read_idx = 0
    buf_count = 0
    download_finished = False
    playback_finished = False
    
    # メモリ掃除
    gc.collect()
    
    # 2. ダウンロードスレッド開始
    # MicroPythonの仕様上、スレッドは使い捨てにするのが安全
    _thread.start_new_thread(download_task, ())
    
    # 3. バッファリング待ち (少し溜まるまで待つ)
    print("Buffering...", end="")
    timeout = 0
    while buf_count < 8000 and not download_finished:
        time.sleep(0.1)
        timeout += 1
        if timeout > 50: break # タイムアウト(5秒)
    print("Start!")
    led.off()
    
    # 4. 再生開始
    player.start(8000)
    
    # 5. 終了待ちループ
    while not playback_finished:
        time.sleep(0.1)
        
    # 6. 停止
    player.stop()
    print("Playback Finished")
    led.on()
    time.sleep(0.5) # 完了後の安定待ち

# --- メインループ ---
def main():
    connect_wifi()
    player = AudioPlayer(PWM_PIN)
    
    print("System Ready. Waiting for sensor trigger...")
    
    try:
        while True:
            # 1. 距離計測
            dist = read_distance()
            # print(f"Dist: {dist:.1f}cm") # デバッグ用
            
            # 2. トリガー判定
            if 0 < dist < THRESHOLD_TRIGGER:
                print(f"\nTriggered! (Dist: {dist:.1f}cm)")
                
                # 再生実行
                play_one_shot(player)
                
                # 3. 再生終了後、人が離れるまで待機 (リセット待ち)
                print("Waiting for reset...")
                while True:
                    d = read_distance()
                    if d > THRESHOLD_RESET:
                        print("Sensor Reset. Ready.")
                        break
                    time.sleep(0.5)
            
            time.sleep(0.2) # 監視間隔
            
    except KeyboardInterrupt:
        player.stop()
        print("Stopped")

if __name__ == "__main__":
    main()