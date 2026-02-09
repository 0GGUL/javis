import streamlit as st
import pyupbit
import pandas as pd
import numpy as np
import time
import requests
import logging
import math
import warnings
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta

# [1. 설정]
warnings.filterwarnings("ignore", category=UserWarning, module='bs4')
warnings.filterwarnings("ignore", category=DeprecationWarning)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
load_dotenv()

# [버전] V15.9.39 Final (존재하는 코인도 조회 실패 시 봇 꺼짐 방지 패치)
st.set_page_config(page_title="자비스 V15.9.39 Final", page_icon="🦅", layout="wide")

# [2. 세션 초기화]
if 'quant_report' not in st.session_state: st.session_state['quant_report'] = {} 
if 'last_scan_msg' not in st.session_state: st.session_state['last_scan_msg'] = None
if 'trailing_peaks' not in st.session_state: st.session_state['trailing_peaks'] = {}
if 'last_scan_time' not in st.session_state: st.session_state['last_scan_time'] = 0
if 'monitored_coins' not in st.session_state: st.session_state['monitored_coins'] = []
if 'wallet_snapshot' not in st.session_state: st.session_state['wallet_snapshot'] = []

# [3. API]
access_key = os.getenv("UPBIT_ACCESS_KEY")
secret_key = os.getenv("UPBIT_SECRET_KEY")
tele_token = os.getenv("TELEGRAM_TOKEN")
tele_id = os.getenv("TELEGRAM_CHAT_ID")

def fmt_price(price):
    if price < 1: return f"{price:,.4f}원"
    elif price < 100: return f"{price:,.2f}원"
    else: return f"{price:,.0f}원"

# -----------------------------------------------------------------------------
# [기능] 텔레그램
# -----------------------------------------------------------------------------
def send_telegram_message(text):
    if not tele_token or not tele_id: return
    try:
        url = f"https://api.telegram.org/bot{tele_token}/sendMessage"
        params = {'chat_id': tele_id, 'text': text, 'parse_mode': 'Markdown'}
        requests.get(url, params=params)
    except: pass

# -----------------------------------------------------------------------------
# [기능] 매수/매도 로직
# -----------------------------------------------------------------------------
def execute_buy_logic(ticker, buy_amount, cut_trigger, strategy_name):
    try:
        upbit = pyupbit.Upbit(access_key, secret_key)
        curr_cash = upbit.get_balance("KRW")
        
        if curr_cash < buy_amount: buy_amount = curr_cash * 0.999
        if buy_amount < 5000: return False, f"잔액 부족 (최소 5000원 필요)"

        buy_res = upbit.buy_market_order(ticker, buy_amount)
        if 'error' in buy_res: return False, f"매수 실패: {buy_res}"
        
        msg = (
            f"🦅 **자비스 매수 체결 (V15.9.39)**\n\n"
            f"🎯 종목: {ticker}\n"
            f"💡 등급: {strategy_name}\n"
            f"💰 투입: {buy_amount:,.0f}원\n"
            f"🛡️ 손절가: {fmt_price(cut_trigger)}"
        )
        send_telegram_message(msg)
        return True, "SUCCESS"
    except Exception as e:
        return False, str(e)

def sell_all_holdings():
    try:
        upbit = pyupbit.Upbit(access_key, secret_key)
        balances = upbit.get_balances()
        sold_count = 0
        for b in balances:
            if b['currency'] == 'KRW': continue
            ticker = f"KRW-{b['currency']}"
            volume = float(b['balance']) + float(b['locked'])
            curr = pyupbit.get_current_price(ticker)
            if volume * curr > 5000:
                upbit.sell_market_order(ticker, volume)
                sold_count += 1
                time.sleep(0.1)
        if sold_count > 0: send_telegram_message(f"🧹 전체 청산 완료 ({sold_count}종목)")
        return sold_count
    except: return 0

# -----------------------------------------------------------------------------
# [엔진 1] 시장 날씨
# -----------------------------------------------------------------------------
def analyze_market_weather():
    try:
        btc_df = pyupbit.get_ohlcv("KRW-BTC", interval="day", count=20)
        if btc_df is None or len(btc_df) < 20: return 0, 0, 0
        curr_price = btc_df['close'].iloc[-1]
        ma5 = btc_df['close'].rolling(5).mean().iloc[-1] 
        change_rate = (btc_df['close'].iloc[-1] - btc_df['open'].iloc[-1]) / btc_df['open'].iloc[-1] * 100
        return curr_price, ma5, change_rate
    except: return 0, 0, 0

# -----------------------------------------------------------------------------
# [엔진 2] 👁️ 호가창 X-Ray
# -----------------------------------------------------------------------------
def analyze_orderbook_depth(ticker):
    try:
        ob = pyupbit.get_orderbook(ticker)
        if not ob: return 0, False, False
        units = ob['orderbook_units'][:5]
        ask_vol = sum([u['ask_size'] for u in units]) 
        bid_vol = sum([u['bid_size'] for u in units]) 
        if ask_vol == 0: ask_vol = 0.0001
        ratio = bid_vol / ask_vol
        is_fake_wall = False
        if ratio > 5.0: is_fake_wall = True
        top_bid = units[0]['bid_size']
        avg_bid = bid_vol / 5
        is_real_wall = (top_bid > avg_bid * 2) and (not is_fake_wall)
        return ratio, is_real_wall, is_fake_wall
    except: return 0, False, False

# -----------------------------------------------------------------------------
# [엔진 3] 👁️ 지표 계산 (OBV 포함)
# -----------------------------------------------------------------------------
def calculate_god_indicators(df):
    try:
        v = df['volume']
        tp = (df['high'] + df['low'] + df['close']) / 3
        mf = tp * v
        
        pos_flow = []; neg_flow = []
        for i in range(len(df)):
            if i == 0: pos_flow.append(0); neg_flow.append(0); continue
            if tp.iloc[i] > tp.iloc[i-1]: pos_flow.append(mf.iloc[i]); neg_flow.append(0)
            elif tp.iloc[i] < tp.iloc[i-1]: pos_flow.append(0); neg_flow.append(mf.iloc[i])
            else: pos_flow.append(0); neg_flow.append(0)
        
        pos_sum = pd.Series(pos_flow).rolling(14).sum()
        neg_sum = pd.Series(neg_flow).rolling(14).sum()
        mfi = 100 - (100 / (1 + pos_sum / neg_sum))
        
        df = df.assign(vwap=(tp * v).cumsum() / v.cumsum())
        
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        up_vol = df[df['close'] > df['open']]['volume'].sum()
        down_vol = df[df['close'] < df['open']]['volume'].sum()
        if down_vol == 0: down_vol = 1
        trade_strength = (up_vol / down_vol) * 100
        
        if len(df) >= 20: ma20 = df['close'].rolling(window=20).mean().iloc[-1]
        else: ma20 = df['close'].mean()
        if math.isnan(ma20): ma20 = 0

        obv = [0] * len(df)
        for i in range(1, len(df)):
            if df['close'].iloc[i] > df['close'].iloc[i-1]:
                obv[i] = obv[i-1] + df['volume'].iloc[i]
            elif df['close'].iloc[i] < df['close'].iloc[i-1]:
                obv[i] = obv[i-1] - df['volume'].iloc[i]
            else:
                obv[i] = obv[i-1]
        df['obv'] = obv

        price_slope = df['close'].iloc[-1] - df['close'].iloc[-5]
        mfi_slope = mfi.iloc[-1] - mfi.iloc[-5]
        is_divergence = False
        if price_slope <= 0 and mfi_slope > 5: is_divergence = True
            
        return mfi.iloc[-1], df['vwap'].iloc[-1], is_divergence, rsi.iloc[-1], trade_strength, ma20, df
    except: return 50, 0, False, 50, 0, 0, df

def get_risk_tickers():
    try:
        all = pyupbit.get_market_all(is_details=True)
        return [m['market'] for m in all if m['market_warning'] != 'NONE']
    except: return []

# -----------------------------------------------------------------------------
# [엔진 4] 듀얼 코어 분석
# -----------------------------------------------------------------------------
def analyze_quant_coin(ticker):
    try:
        df = pyupbit.get_ohlcv(ticker, interval="minute15", count=100)
        if df is None or len(df) < 20: return None
        
        row = df.iloc[-1]
        close = row['close']
        open_p = row['open']
        high_p = row['high']
        volume = row['volume']
        
        mfi, vwap, is_divergence, rsi, strength, ma20, df_full = calculate_god_indicators(df)
        ratio, is_wall, is_fake_wall = analyze_orderbook_depth(ticker)
        
        avg_vol = df['volume'].rolling(20).mean().iloc[-1]
        rvol = volume / avg_vol if avg_vol > 0 else 0

        if is_fake_wall: return None 
        if rsi >= 70: return None 

        score = 0
        reasons = []
        strategy_type = ""

        # [전략 A] 스나이퍼
        if close > ma20:
            if ma20 > 0 and close <= ma20 * 1.03:
                sniper_score = 0
                if close > ma20: sniper_score += 40
                if strength >= 100: sniper_score += 20
                if rvol >= 2.0: sniper_score += 20
                if is_divergence: sniper_score += 10
                
                if sniper_score >= 70:
                    strategy_type = "🔫추세포착"
                    score = sniper_score
                    reasons.append("정배열 돌파")
                    reasons.append(f"강도{int(strength)}%")

        # [전략 B] 잠입
        if not strategy_type: 
            recent_df = df_full.iloc[-20:]
            max_price = recent_df['close'].max()
            current_obv = recent_df['obv'].iloc[-1]
            max_obv = recent_df['obv'].max()
            
            if close < max_price * 0.98:
                if current_obv >= max_obv * 0.99: 
                    strategy_type = "🕵️세력매집"
                    score = 85 
                    reasons.append("가격횡보중")
                    reasons.append("OBV상승(매집)")

        if not strategy_type or score < 70: return None
        
        body = abs(close - open_p)
        upper_shadow = high_p - max(close, open_p)
        if body > 0 and upper_shadow > body * 2: return None

        reasons.insert(0, strategy_type)
        cut_price = vwap * 0.97
        target_price = close * 1.03

        return {
            't': ticker, 'p': close, 'prob': score,
            'reasons': ", ".join(reasons),
            'pos_ratio': 0.3, 'cut': cut_price, 'target': target_price,
            'vwap': vwap, 'divergence': is_divergence, 'rsi': rsi,
            'strength': strength, 'ma20': ma20, 'found_time': datetime.now()
        }
    except: return None

# [핵심] 스캔 로직 (투명 인간 모드 + 500원 수익 보장형 배팅)
def scan_whole_market(total_cash, auto_mode=False, target_list=None, auto_buy=False):
    try:
        upbit_check = pyupbit.Upbit(access_key, secret_key)
        balances = upbit_check.get_balances()
        
        # 1. 1차 필터: 장부상 보유 종목 확인
        held_tickers = []
        for b in balances:
            total_held_qty = float(b['balance']) + float(b['locked'])
            # 1000원 이상이면 '보유 중'으로 판단 (알림 차단용)
            if b['currency'] != 'KRW' and total_held_qty * float(b['avg_buy_price']) > 1000:
                held_tickers.append(f"KRW-{b['currency']}")

        # [NEW] 투명 인간 처리 (LINK, ERA는 카운트에서 제외)
        ghost_tickers = ['KRW-LINK', 'KRW-ERA']
        active_count = 0
        for t in held_tickers:
            if t not in ghost_tickers:
                active_count += 1

        status_log = f"👁️ 분석 중... (보유 {len(held_tickers)}개 / 유효 {active_count}개)"
        if active_count >= 3 and auto_buy:
            status_log = f"👁️ 분석 중... (유효 3개 달성 ➔ 자동매수 일시정지)"

        if target_list and len(target_list) > 0: tickers = target_list
        else: tickers = pyupbit.get_tickers(fiat="KRW")
            
        risk_tickers = get_risk_tickers()
        
        if not auto_mode:
            my_bar = st.progress(0, text=status_log)
        else:
            msg_log = "🔄 초고속 감시 중..."
        
        current_data = pyupbit.get_current_price(tickers, verbose=True)
        if not isinstance(current_data, dict): pass

        cnt = 0
        new_findings = []
        
        for i, t in enumerate(tickers):
            try:
                df_mini = pyupbit.get_ohlcv(t, interval="minute15", count=2)
                if df_mini is None or len(df_mini) < 2: continue
            except: continue

            if not auto_mode: my_bar.progress((i + 1) / len(tickers), text=f"{status_log} - {t}")

            res = analyze_quant_coin(t)
            
            if res:
                if t in risk_tickers: res['t'] = f"⚠️ {res['t']}"
                
                # =========================================================
                # 💰 [500원 수익 보장형 배팅] (약 1.7만 원 최소값)
                # =========================================================
                min_seed_for_profit = 17000
                
                if res['prob'] >= 90:
                    bet_ratio = 0.5  # VIP: 50%
                    strategy_label = "👑VIP"
                else:
                    bet_ratio = 0.1  # 일반: 10%
                    strategy_label = "🔫일반"
                
                calc_amount = total_cash * bet_ratio
                final_bet = max(calc_amount, min_seed_for_profit)
                final_bet = min(final_bet, total_cash * 0.999) 
                
                res['bet_money'] = final_bet
                st.session_state['quant_report'][res['t']] = res
                new_findings.append(res)

                # 자동 매수 (LINK, ERA 제외한 카운트로 체크)
                is_green_light = res['p'] >= res['ma20']
                can_auto_buy = active_count < 3
                
                if auto_buy and can_auto_buy and res['prob'] >= 70 and res['strength'] >= 100.0 and is_green_light:
                    final_reason_tag = f"{strategy_label} + {res['reasons'].split(',')[0]}"
                    execute_buy_logic(res['t'], res['bet_money'], res['cut'], final_reason_tag)
                    res['reasons'] = "🤖자동매수 + " + res['reasons']
            
            cnt += 1
            if cnt % 50 == 0: time.sleep(0.1)
            
        if not auto_mode: my_bar.empty()
        
        current_time = datetime.now()
        expired_keys = []
        for k, v in st.session_state['quant_report'].items():
            if (current_time - v['found_time']).total_seconds() > 3600: expired_keys.append(k)
        for k in expired_keys: del st.session_state['quant_report'][k]

        if auto_mode and new_findings:
            new_findings.sort(key=lambda x: x['prob'], reverse=True)
            best = new_findings[0]
            
            clean_ticker_name = best['t'].replace('⚠️ ', '')
            is_just_bought = "🤖자동매수" in best['reasons']
            
            # [알림] 보유 여부 2중 체크 (여긴 실제 보유 리스트인 held_tickers 사용 -> 중복매수 방지)
            if clean_ticker_name not in held_tickers and not is_just_bought:
                try:
                    real_bal = upbit_check.get_balance(clean_ticker_name)
                    if real_bal is None: real_bal = 0.0
                except: real_bal = 0.0
                
                # 1,000원 미만일 때만 알림
                if real_bal * best['p'] < 1000:
                    if (datetime.now() - best['found_time']).total_seconds() < 60:
                        strategy_type = best['reasons'].split(',')[0]
                        tele_msg = (
                            f"🦅 **자비스 사냥 성공 (V15.9.39)**\n\n"
                            f"💎 종목: {best['t']}\n"
                            f"🧭 등급: {'👑VIP' if best['prob']>=90 else '🔫일반'}\n"
                            f"📊 점수: {best['prob']}점 (강도 {int(best['strength'])}%)\n"
                            f"💰 추천금: {best['bet_money']:,.0f}원\n"
                        )
                        send_telegram_message(tele_msg)

        report_list = list(st.session_state['quant_report'].values())
        report_list.sort(key=lambda x: x['found_time'], reverse=True)

        return report_list, status_log
    except Exception as e: return [], f"오류: {e}"

def get_full_asset_info():
    try:
        upbit = pyupbit.Upbit(access_key, secret_key)
        balances = upbit.get_balances()
        portfolio = []
        total_krw = 0
        total_assets = 0
        
        for b in balances:
            if b['currency'] == 'KRW':
                total_krw = float(b['balance']) + float(b['locked'])
                total_assets += total_krw
                continue
                
            ticker = f"KRW-{b['currency']}"
            amount = float(b['balance']) + float(b['locked'])
            if amount == 0: continue
            
            avg = float(b['avg_buy_price'])
            curr = pyupbit.get_current_price(ticker)
            if not curr: curr = avg
            val = amount * curr
            total_assets += val
            profit_pct = (curr - avg) / avg * 100
            
            if ticker not in st.session_state['trailing_peaks']:
                st.session_state['trailing_peaks'][ticker] = curr
            else:
                if curr > st.session_state['trailing_peaks'][ticker]:
                    st.session_state['trailing_peaks'][ticker] = curr
            
            peak = st.session_state['trailing_peaks'][ticker]
            drop_rate = (peak - curr) / peak * 100
            
            should_sell = False
            reason = ""
            
            # [익절 로직] 3.0% (약 500원 수익) 넘으면 감시 시작 -> 고점 대비 1.5% 빠지면 매도
            if curr < avg * 0.97: should_sell = True; reason = "🚨 손절 (-3%)"
            elif profit_pct >= 3.0 and drop_rate >= 1.5: should_sell = True; reason = f"💰 익절 (고점 대비 -1.5% 반납)"

            if not should_sell and profit_pct < 0.5:
                try:
                    ob = pyupbit.get_orderbook(ticker)
                    if ob:
                        ask_total = sum([u['ask_size'] for u in ob['orderbook_units'][:5]])
                        bid_total = sum([u['bid_size'] for u in ob['orderbook_units'][:5]])
                        if bid_total < ask_total * 0.2: should_sell = True; reason = "📉 방어벽 붕괴 (세력 이탈 감지)"
                except: pass

            portfolio.append({
                "종목": ticker, "수익률": profit_pct, "평가금액": val, 
                "should_sell": should_sell, "reason": reason, "보유수량": amount
            })
            
        return total_krw, total_assets, portfolio
    except: return 0, 0, []

# -----------------------------------------------------------------------------
# [UI]
# -----------------------------------------------------------------------------
st.title("🦅 자비스 V15.9.39 Final")
st.caption("Ghost 모드 + 500원 보장 + KeyError 완벽 방어(개별조회)")

# 1. 자산 계산
my_cash, my_total, my_portfolio = get_full_asset_info()
btc_price, btc_ma5, btc_change = analyze_market_weather()

# [자동 등록 로직]
current_tickers = [p['종목'] for p in my_portfolio]
if not st.session_state['wallet_snapshot']:
    st.session_state['wallet_snapshot'] = current_tickers
    if not st.session_state['monitored_coins']:
        st.session_state['monitored_coins'] = current_tickers

newly_bought_coins = [t for t in current_tickers if t not in st.session_state['wallet_snapshot']]
if newly_bought_coins:
    for nc in newly_bought_coins:
        if nc not in st.session_state['monitored_coins']:
            st.session_state['monitored_coins'].append(nc)
            send_telegram_message(f"🔭 **[자비스] 신규 감시 등록**\n\n✅ {nc} 종목을 자동 매도 대상에 추가했습니다.")
    st.session_state['wallet_snapshot'] = current_tickers

st.session_state['wallet_snapshot'] = [t for t in st.session_state['wallet_snapshot'] if t in current_tickers]
st.session_state['monitored_coins'] = [t for t in st.session_state['monitored_coins'] if t in current_tickers]

c1, c2, c3 = st.columns(3)
c1.metric("총 자산", f"{my_total:,.0f} 원")
c2.metric("가용 현금", f"{my_cash:,.0f} 원")
c3.metric("BTC 현재가", fmt_price(btc_price), f"{btc_change:.2f}%")

st.markdown("---")

auto_refresh = st.sidebar.checkbox("💓 화면 자동 새로고침", value=True)
st.sidebar.markdown("---")
enable_auto_scan = st.sidebar.checkbox("🔭 집중 감시 모드 (알림)", value=False)
scan_interval_min = st.sidebar.selectbox("⏱️ 알림 주기 설정", [1, 3, 5, 10], index=1)
if enable_auto_scan: st.sidebar.success(f"✅ {scan_interval_min}분마다 초고속 스캔 중...")

st.sidebar.markdown("---")
auto_trade = st.sidebar.checkbox("✅ 자동 매도 활성화 (Master)", value=False)
auto_buy = st.sidebar.checkbox("🚀 자동 매수 (70점/강도100%/초록불)", value=False)

target_coins = []
if current_tickers:
    st.sidebar.markdown("### 🎯 집중 관리 대상 설정")
    target_coins = st.sidebar.multiselect(
        "감시할 종목 (자동 동기화됨):", 
        current_tickers,
        default=st.session_state['monitored_coins'],
        key='target_selector'
    )
    st.session_state['monitored_coins'] = target_coins

    if auto_trade:
        if target_coins: st.sidebar.caption(f"🔥 {len(target_coins)}개 종목 집중 케어 중...")
        else: st.sidebar.warning("선택된 종목이 없습니다! (자동 매도 안 함)")

if st.sidebar.button("🔄 수동 새로고침"): st.rerun()

st.subheader("💼 현재 포지션")
if my_portfolio:
    for p in my_portfolio:
        is_target = p['종목'] in target_coins
        
        # [핵심] 매도 주문 실패 시 알림 안 보내도록 로직 수정
        if auto_trade and is_target and p['should_sell']:
            upbit = pyupbit.Upbit(access_key, secret_key)
            res = upbit.sell_market_order(p['종목'], p['보유수량'])
            
            # 매도 주문이 성공적으로 들어갔을 때만(UUID가 있을 때만) 알림 전송
            if res and 'uuid' in res:
                send_telegram_message(f"⚡ 자동 매도 실행: {p['종목']} ({p['reason']})")
                st.rerun()
            else:
                pass

        with st.expander(f"{p['종목']} ({p['수익률']:.2f}%) {'🎯' if is_target else '💤'}"):
            col1, col2 = st.columns(2)
            col1.write(f"평가금: {p['평가금액']:,.0f}원")
            
            status_text = "✅ 홀딩 중"
            if p['should_sell']: status_text = f"⚠️ 매도 신호 ({p['reason']})"
            
            if not is_target: status_text += " (⛔ 매도 제외됨)"
            else: status_text += " (👀 감시 중)"
            
            col1.write(f"상태: {status_text}")
            if col2.button("수동 매도", key=p['종목']):
                pyupbit.Upbit(access_key, secret_key).sell_market_order(p['종목'], p['보유수량'])
                st.success("매도 완료")
                st.rerun()
else:
    st.info("보유 종목이 없습니다.")

st.markdown("---")

current_time = datetime.now()
expired_keys = []
for k, v in st.session_state['quant_report'].items():
    if (current_time - v['found_time']).total_seconds() > 3600: expired_keys.append(k)
for k in expired_keys: del st.session_state['quant_report'][k]
if expired_keys: st.rerun()

st.subheader(f"🔭 세력 감시 타임라인 (V15.9.39)")
c_btn1, c_btn2 = st.columns(2)
if c_btn1.button("👁️ 즉시 수동 분석 (목록 갱신)", type="primary", use_container_width=True):
    report_list, log = scan_whole_market(my_cash, auto_mode=False, auto_buy=auto_buy)
    st.session_state['last_scan_msg'] = log

if c_btn2.button("🗑️ 목록 비우기", use_container_width=True):
    st.session_state['quant_report'] = {}
    st.rerun()

report_view = list(st.session_state['quant_report'].values())
report_view.sort(key=lambda x: x['found_time'], reverse=True)

if report_view:
    st.info(st.session_state['last_scan_msg'])
    
    # [HOTFIX] 가격 조회 로직 전면 수정 (개별 조회 + 에러 무시)
    current_prices = {}
    for r in report_view:
        clean_ticker = str(r['t']).replace('⚠️ ', '').strip()
        try:
            # 개별로 하나씩 조회 (에러 나면 걔만 패스)
            cp_data = pyupbit.get_current_price(clean_ticker)
            if isinstance(cp_data, (int, float)):
                current_prices[clean_ticker] = cp_data
        except:
            pass # PUMP/BEAM 등 조회 안 되는 놈은 그냥 무시 (0으로 처리됨)

    for idx, r in enumerate(report_view):
        elapsed = (datetime.now() - r['found_time']).total_seconds() / 60
        clean_ticker = str(r['t']).replace('⚠️ ', '').strip()
        
        # 조회 실패 시 -> 기존 발견 가격(r['p']) 그대로 사용
        curr_p = current_prices.get(clean_ticker, r['p'])
        
        found_p = r['p']
        diff_pct = (curr_p - found_p) / found_p * 100
        
        status_color = "🟢"; status_msg = f"진입 추천 (점수 {r['prob']}점)"
        if curr_p < r['ma20']: status_color = "🔴"; status_msg = "위험: 추세 이탈"
        elif r['rsi'] >= 70: status_color = "🔴"; status_msg = f"위험: 심리 과열 (RSI {int(r['rsi'])})"
        elif diff_pct >= 2.0: status_color = "🟡"; status_msg = "관망: 이미 상승함"
        elif curr_p < r['cut']: status_color = "🔴"; status_msg = "진입 금지: 손절가 이탈"
        elif diff_pct < 0: status_msg = f"강력 추천: 눌림목 기회 (점수 {r['prob']}점)"
            
        with st.container():
            strategy_title = "👑VIP" if r['prob'] >= 90 else "🔫일반"
            st.markdown(f"### {status_color} [{strategy_title}] {r['t']} <small style='color:gray'>({int(elapsed)}분 전)</small>", unsafe_allow_html=True)
            st.progress(r['prob']/100, text=f"점수: {r['prob']}점 / 강도: {int(r['strength'])}%")
            if status_color == "🟢": st.success(f"**분석 결과:** {status_msg}")
            elif status_color == "🟡": st.warning(f"**분석 결과:** {status_msg}")
            else: st.error(f"**분석 결과:** {status_msg}")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("발견 당시", fmt_price(r['p']))
            c2.metric("현재 실시간", fmt_price(curr_p), f"{diff_pct:.2f}%")
            c3.metric("목표 익절", fmt_price(r['target']))
            c4.metric("추천 매수금", f"{r['bet_money']:,.0f}원")
            
            with st.expander("📌 세력 분석 리포트", expanded=False):
                st.markdown(f"""
                - **감지 전략:** {r['reasons'].split(',')[0]}
                - **세력 강도:** {int(r['strength'])}%
                - **전략 구분:** {strategy_title} (비중 {r['bet_money']:,.0f}원)
                """)
            if st.button(f"매수 ({r['t']})", key=f"buy_{r['t']}"):
                execute_buy_logic(r['t'], r['bet_money'], r['cut'], r['reasons'].split(',')[0])
                st.success(f"{r['bet_money']:,.0f}원 매수 주문 완료")
                time.sleep(1)
                st.rerun()
            st.markdown("---")
elif st.session_state['last_scan_msg']:
    st.warning("🔭 세력 추적 중... (VIP 50% vs 일반 10%)")

if enable_auto_scan:
    curr_ts = time.time()
    last_ts = st.session_state['last_scan_time']
    interval_sec = 30 
    
    if curr_ts - last_ts > interval_sec:
        with st.spinner(f"🦅 자비스가 1차 예선을 진행 중입니다..."):
            scan_targets = target_coins if target_coins else None
            # [NEW] auto_buy 전달
            report_list, log = scan_whole_market(my_cash, auto_mode=True, target_list=scan_targets, auto_buy=auto_buy)
            st.session_state['last_scan_msg'] = f"🔄 감시 완료 ({datetime.now().strftime('%H:%M:%S')})"
            st.session_state['last_scan_time'] = curr_ts
        st.rerun()

with st.sidebar:
    if st.button("🚨 전체 청산"): sell_all_holdings(); st.rerun()
if auto_refresh: time.sleep(5); st.rerun()