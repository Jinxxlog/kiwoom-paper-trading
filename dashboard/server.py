import json
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from config.settings import (
    load_ai_shadow_settings,
    load_candidate_settings,
    load_database_path,
    load_information_settings,
    load_market_mode_gate_settings,
    load_market_data_settings,
    load_paper_trading_settings,
    load_risk_settings,
    load_settings,
    load_strategy_settings,
)
from data.api_budget import ApiBudgetManager
from data.market_calendar import MarketCalendar, SEOUL_TIMEZONE
from ai_shadow.service import AiShadowService
from db.database import Database
from kiwoom_rest_client import KiwoomRestClient
from information.service import InformationService
from operations.runtime_control import CollectorRuntimeControl
from operations.market_mode import MarketModeService
from operations.universe_runtime import UniverseCollectionRuntime
from operations.service import OperationsService
from reports.trading_review import TradingReviewReport
from screening.engine import CandidateScreeningEngine
from screening.intraday import IntradayVwapService
from screening.leader_learning import LeaderOutcomeReportService
from screening.stock_category import STOCK_CATEGORY_LABELS, stock_category_payload
from strategy.registry import register_breakout_strategy, register_risk_strategy
from strategy.session_profile import (
    ALWAYS_ACTIVE_PROFILE,
    intraday_strategy_mode_for,
    session_profile_for,
)


HTML = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>자동투자 운영 대시보드</title>
<style>
:root { color-scheme: dark; font-family: Inter, Pretendard, system-ui, sans-serif; }
* { box-sizing: border-box; }
body { margin: 0; background: #080d19; color: #e8edf7; }
main { max-width: 1180px; margin: auto; padding: 28px 18px 60px; }
h1 { margin: 0 0 6px; font-size: 28px; }
h2 { margin: 0; font-size: 18px; }
.sub { color: #94a3b8; margin-bottom: 22px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(220px,1fr)); gap: 14px; }
.card { background: #131a2b; border: 1px solid #26314d; border-radius: 14px; padding: 17px; }
.card-title { display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:13px; }
.label { color: #94a3b8; font-size: 13px; }
.value { font-size: 21px; font-weight: 700; margin-top: 7px; }
.metric { font-size: 24px; font-weight: 750; margin: 5px 0; }
.muted { color:#94a3b8; }
.ok { color: #4ade80; } .warn { color: #fbbf24; } .bad { color: #fb7185; }
.controls { display: flex; flex-wrap: wrap; gap: 12px; align-items: end; margin: 14px 0 4px; }
label { display: grid; gap: 5px; color: #cbd5e1; font-size: 13px; }
input[type=text], input[type=number], input[type=date], select { background:#080d19; color:#fff; border:1px solid #364361; border-radius:8px; padding:9px; min-height:38px; }
input[type=checkbox] { width:18px; height:18px; }
button { border:0; border-radius:9px; padding:10px 16px; font-weight:700; cursor:pointer; }
.start { background:#22c55e; color:#06230f; } .stop { background:#f43f5e; color:#fff; }
.secondary { background:#26314d; color:#e8edf7; }
.filter-bar { display:grid; grid-template-columns:repeat(5,minmax(120px,1fr)) auto; gap:10px; align-items:end; margin-bottom:12px; }
.filter-actions { display:flex; gap:8px; align-items:center; }
.tabs { display:flex; gap:8px; margin:22px 0 14px; border-bottom:1px solid #26314d; }
.tab { background:transparent; color:#94a3b8; border-radius:10px 10px 0 0; padding:12px 18px; }
.tab.active { color:#fff; background:#1b2540; box-shadow:inset 0 -2px #60a5fa; }
.panel { display:none; } .panel.active { display:block; }
.badge { border:1px solid #3b4968; border-radius:999px; padding:4px 9px; font-size:12px; color:#cbd5e1; }
.notice { border-left:3px solid #60a5fa; padding:11px 13px; background:#111b30; color:#cbd5e1; border-radius:7px; }
.danger-notice { border-left-color:#fb7185; }
.account-row { padding:12px 0; border-bottom:1px solid #26314d; }
.account-row:last-child { border-bottom:0; }
.strategy-row { padding:12px 0; border-bottom:1px solid #26314d; }
.strategy-row:last-child { border-bottom:0; }
.strategy-summary { margin-top:4px; color:#cbd5e1; font-size:13px; line-height:1.45; }
.flow-top { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:10px; margin-bottom:12px; }
.flow-metric { background:#0d1424; border:1px solid #26314d; border-radius:8px; padding:10px; min-height:72px; }
.flow-metric b { display:block; font-size:18px; margin-top:4px; }
.flow-list { display:grid; gap:8px; }
.flow-item { border-top:1px solid #26314d; padding-top:9px; }
.pnl-positive { color:#4ade80; }
.pnl-negative { color:#fb7185; }
.pnl-flat { color:#cbd5e1; }
.chip { display:inline-block; border-radius:999px; padding:2px 8px; font-size:12px; font-weight:700; background:#1b2540; color:#cbd5e1; }
.chip.ok { background:#064e3b; color:#86efac; }
.chip.warn { background:#422006; color:#facc15; }
.chip.bad { background:#4c0519; color:#fda4af; }
.replay-chart { margin-top:8px; max-width:520px; }
.replay-chart svg { width:100%; height:150px; display:block; background:#0d1424; border:1px solid #26314d; border-radius:8px; }
.replay-outcomes { margin-top:5px; display:flex; flex-wrap:wrap; gap:5px; }
table { width:100%; border-collapse:collapse; font-size:14px; }
th,td { text-align:left; padding:10px 7px; border-bottom:1px solid #26314d; }
th { color:#94a3b8; font-weight:600; }
.candidate-table th:nth-child(3),
.candidate-table td:nth-child(3),
.candidate-table th:nth-child(8),
.candidate-table td:nth-child(8),
.candidate-table th:nth-child(9),
.candidate-table td:nth-child(9),
.candidate-table th:nth-child(10),
.candidate-table td:nth-child(10),
.candidate-table th:nth-child(11),
.candidate-table td:nth-child(11),
.candidate-table th:nth-child(12),
.candidate-table td:nth-child(12) { display:none; }
.scroll { overflow-x:auto; }
.message { min-height: 22px; color:#fbbf24; margin-top:8px; }
@media (max-width:760px) {
  main { padding:20px 12px 40px; }
  .tab { flex:1; padding:11px 8px; }
  .filter-bar { grid-template-columns:repeat(2,minmax(0,1fr)); }
  .filter-actions { grid-column:1 / -1; }
  .filter-actions button { flex:1; }
}
</style>
</head>
<body><main>
<h1>자동투자 운영 대시보드</h1>
<div class="sub">로컬 PC 전용 · 실전 주문 기능 없음 · 5초마다 자동 갱신</div>
<div class="grid">
  <div class="card"><div class="label">자동매매 프로세스</div><div id="runtime" class="value">확인 중</div></div>
  <div class="card"><div class="label">키움 API 설정</div><div id="api" class="value">확인 중</div></div>
  <div class="card"><div class="label">현재 장 상태</div><div id="market" class="value">확인 중</div></div>
  <div class="card"><div class="label">시장 모드 게이트</div><div id="market-mode-gate" class="value">확인 중</div></div>
  <div class="card"><div class="label">최근 가격 수집</div><div id="collection" class="value">확인 중</div></div>
</div>

<div class="tabs" role="tablist">
  <button id="paper-tab" class="tab active" onclick="showTab('paper')">모의투자</button>
  <button id="performance-tab" class="tab" onclick="showTab('performance')">손익</button>
  <button id="theme-trends-tab" class="tab" onclick="showTab('theme-trends')">30일 주도테마</button>
  <button id="leader-tab" class="tab" onclick="showTab('leader')">한달 테마주</button>
  <button id="live-tab" class="tab" onclick="showTab('live')">실전계좌</button>
</div>

<section id="paper-panel" class="panel active">
  <div class="notice">모의 주문은 기본 돌파 전략을 사용합니다. 뉴스·공시는 후보 점수와 강한 악재 진입 차단에 반영되며 근거가 별도로 저장됩니다.</div>
  <div class="card" style="margin-top:14px">
    <div class="card-title"><h2>무인 시작 전 운영 경고</h2><span id="operation-warning-badge" class="badge">확인 중</span></div>
    <div id="operation-warning-summary" class="flow-top"></div>
    <div id="operation-warning-reasons" class="muted"></div>
  </div>
  <div class="card" style="margin-top:14px">
    <div class="card-title"><h2>모의 자동매매 제어</h2><span class="badge">주문: 모의계좌만</span></div>
    <div class="controls">
      <label>수집 간격(초)<input id="interval" type="number" min="5" value="60"></label>
      <label>모의계좌<input id="account" type="text" value="default"></label>
      <label><span>자동 전략</span><input id="strategy" type="checkbox" checked></label>
      <label><span>소액 공격</span><input id="small-account-aggressive" type="checkbox"></label>
      <label>전략 A/B 실제모드
        <select id="strategy-ab-active-mode">
          <option value="remake" selected>리메이크</option>
          <option value="normal">일반</option>
        </select>
      </label>
      <label><span>호가 수집</span><input id="depth" type="checkbox"></label>
      <label><span>체결강도 수집</span><input id="trades" type="checkbox"></label>
      <button class="start" onclick="startTrader()">모의투자 시작</button>
      <button class="start" onclick="startUnattended()">무인 모의투자 시작</button>
      <button class="stop" onclick="stopTrader()">정상 종료 요청</button>
    </div>
    <div id="message" class="message"></div>
  </div>
  <div class="card" style="margin-top:14px">
    <div class="card-title"><h2>실시간 자동매매 흐름</h2><span id="automation-mode" class="badge">확인 중</span></div>
    <div id="automation-summary" class="flow-top"></div>
    <div class="grid">
      <div>
        <div class="label">최근 전략 평가</div>
        <div id="automation-signals" class="flow-list"></div>
      </div>
      <div>
        <div class="label">최근 모의 주문</div>
        <div id="automation-orders" class="flow-list"></div>
      </div>
    </div>
  </div>
  <div class="grid" style="margin-top:14px">
    <div class="card">
      <div class="card-title"><h2>현재 장중 모드</h2><span id="intraday-mode-badge" class="badge">확인 중</span></div>
      <div id="intraday-mode-summary" class="flow-top"></div>
      <div id="intraday-mode-reason" class="muted"></div>
    </div>
    <div class="card">
      <div class="card-title"><h2>주문 안전장치 상태</h2><span id="order-safety-badge" class="badge">dry-run 포함</span></div>
      <div id="order-safety-summary" class="flow-top"></div>
      <div class="scroll"><table>
        <thead><tr><th>시각</th><th>종목</th><th>모드</th><th>dry-run 차단 사유</th></tr></thead>
        <tbody id="order-safety-events"></tbody>
      </table></div>
    </div>
    <div class="card">
      <div class="card-title"><h2>API 예산 상태</h2><span id="api-budget-badge" class="badge">확인 중</span></div>
      <div id="api-budget-summary" class="flow-top"></div>
      <div id="api-budget-mode" class="muted"></div>
      <div class="scroll"><table>
        <thead><tr><th>그룹 / API</th><th>사용 / 한도</th><th>남은 예산</th><th>backoff</th></tr></thead>
        <tbody id="api-budget-windows"></tbody>
      </table></div>
    </div>
  </div>
  <div class="card" style="margin-top:14px">
    <div class="card-title"><h2>전 종목 등록·과거 일봉 수집</h2><span id="universe-running" class="badge">확인 중</span></div>
    <div id="universe-summary" class="muted"></div>
    <div class="controls">
      <label>목표 일봉 수<input id="target-bars" type="number" min="60" value="200"></label>
      <button class="start" onclick="startUniverseCollection()">자동 등록·수집 시작</button>
      <button class="stop" onclick="stopUniverseCollection()">수집 중지 요청</button>
    </div>
  </div>
  <div class="grid" style="margin-top:14px; display:none">
    <div class="card">
      <div class="card-title"><h2>모의계좌 현황</h2><span id="paper-count" class="badge">0개</span></div>
      <div id="accounts"></div>
    </div>
    <div class="card">
      <div class="card-title"><h2>활성 경보</h2></div>
      <div id="alerts"></div>
    </div>
  </div>
  <div class="grid" style="margin-top:14px">
    <div class="card">
      <div class="card-title"><h2>현재 전략</h2><span id="strategy-active" class="badge">확인 중</span></div>
      <div id="small-account-mode" class="muted" style="margin-bottom:8px">소액 공격 모드: 확인 중</div>
      <div id="strategy-ab-mode" class="muted" style="margin-bottom:8px">전략 A/B 실제모드: 확인 중</div>
      <div id="strategy-versions"></div>
    </div>
    <div class="card">
      <div class="card-title"><h2>위험관리 설정</h2><span class="badge">현재 적용값</span></div>
      <div id="risk-settings"></div>
    </div>
  </div>
  <div class="card" style="margin-top:14px">
    <div class="card-title"><h2>실시간 조회종목 순위</h2><span class="badge">관측 전용 · 점수 미반영</span></div>
    <div id="realtime-rank-summary" class="muted"></div>
    <div class="scroll"><table>
      <thead><tr><th>순위</th><th>종목</th><th>순위변동</th><th>현재가</th><th>기준대비</th><th>관측시각</th></tr></thead>
      <tbody id="realtime-rankings"></tbody>
    </table></div>
  </div>
  <div class="card" style="margin-top:14px">
    <div class="card-title"><h2>기술·패턴 후보 종목</h2><button class="start" onclick="scanCandidates()">후보 다시 계산</button></div>
    <div id="candidate-summary" class="muted"></div>
    <div class="scroll"><table class="candidate-table">
      <thead><tr><th>순위</th><th>종목</th><th>기존점수 / 변화</th><th>종합판단</th><th>기술</th><th>패턴</th><th>뉴스·공시</th><th>상대강도</th><th>RSI / ATR</th><th>감지 패턴</th><th>5분 VWAP</th><th>관심</th><th>선정·제외 사유</th></tr></thead>
      <tbody id="candidate-scores"></tbody>
    </table></div>
  </div>
  <div class="card" style="margin-top:14px; display:none">
    <div class="card-title"><h2>최근 뉴스·공시</h2><span class="badge">점수·차단 근거</span></div>
    <div class="scroll"><table>
      <thead><tr><th>공개 시각</th><th>종목</th><th>출처</th><th>사건</th><th>신뢰도</th><th>감쇠 점수</th><th>진입 차단</th><th>제목</th></tr></thead>
      <tbody id="information-items"></tbody>
    </table></div>
  </div>
  <div class="card" style="margin-top:14px">
    <div class="card-title"><h2>AI Shadow 분석</h2><button class="start" onclick="runAiAnalysis()">상위 후보 AI 분석</button></div>
    <div id="ai-summary" class="muted"></div>
    <div class="scroll"><table>
      <thead><tr><th>시각</th><th>종목</th><th>모델</th><th>알고리즘</th><th>AI</th><th>일치</th><th>근거·위험</th><th>토큰·지연·비용</th><th>사후 성과</th></tr></thead>
      <tbody id="ai-analyses"></tbody>
    </table></div>
  </div>
  <div class="card" style="margin-top:14px">
    <div class="card-title"><h2>수급 Shadow 점수</h2><span id="supply-policy" class="badge">저장 중심</span></div>
    <div id="supply-shadow-summary" class="muted"></div>
    <div id="supply-pipeline-summary" class="muted"></div>
    <div class="scroll"><table>
      <thead><tr><th>평가시각</th><th>종목</th><th>총점</th><th>거래원</th><th>외국계</th><th>투자자</th><th>프로그램</th><th>차단 후보</th></tr></thead>
      <tbody id="supply-shadow-scores"></tbody>
    </table></div>
  </div>
  <div class="card" style="margin-top:14px; display:none">
    <div class="card-title"><h2>보유 종목 청산 상태</h2><span class="badge">동적 위험관리</span></div>
    <div class="scroll"><table>
      <thead><tr><th>계좌·종목</th><th>매수가 / 현재가</th><th>수익률</th><th>최고가</th><th>초기 / 현재 손절</th><th>익절 목표</th><th>분할 매도</th><th>보호 상태</th><th>추세</th><th>예상 청산</th><th>데이터 시각</th></tr></thead>
      <tbody id="risk-positions"></tbody>
    </table></div>
  </div>
  <div class="card" style="margin-top:14px">
    <div class="card-title"><h2>최근 전략 판단</h2><span class="badge">점수·주문 추적</span></div>
    <div class="scroll"><table>
      <thead><tr><th>데이터 시각</th><th>전략</th><th>종목</th><th>판단</th><th>점수</th><th>처리</th><th>주문</th><th>근거</th></tr></thead>
      <tbody id="strategy-decisions"></tbody>
    </table></div>
  </div>
</section>

<section id="performance-panel" class="panel">
  <div class="notice">모의계좌 기준 손익입니다. 오늘 실현손익은 오늘 매도 체결로 확정된 금액이고, 평가손익은 현재 보유 종목의 최신 시세 기준입니다.</div>
  <div class="grid" style="margin-top:14px">
    <div class="card"><div class="label">최근 청산 결과</div><div id="perf-last-result" class="metric">-</div><div id="perf-last-detail" class="muted">-</div></div>
    <div class="card"><div class="label">오늘 실현손익</div><div id="perf-realized" class="metric">-</div><div id="perf-realized-detail" class="muted">매도 체결 기준</div></div>
    <div class="card"><div class="label">현재 평가손익</div><div id="perf-unrealized" class="metric">-</div><div id="perf-unrealized-detail" class="muted">보유 종목 기준</div></div>
    <div class="card"><div class="label">오늘 손익 합계</div><div id="perf-total" class="metric">-</div><div id="perf-total-detail" class="muted">실현 + 현재 평가</div></div>
  </div>
  <div class="grid" style="margin-top:14px">
    <div class="card">
      <div class="card-title"><h2>총 계좌 변동</h2><span class="badge">모의계좌</span></div>
      <div id="perf-accounts"></div>
    </div>
    <div class="card">
      <div class="card-title"><h2>보유 평가손익</h2><span class="badge">최신 시세</span></div>
      <div class="scroll"><table>
        <thead><tr><th>종목</th><th>수량</th><th>평균단가</th><th>현재가</th><th>평가손익</th><th>수익률</th></tr></thead>
        <tbody id="perf-positions"></tbody>
      </table></div>
    </div>
  </div>
  <div class="card" style="margin-top:14px">
    <div class="card-title"><h2>진입 유형별 성과</h2><span class="badge">매매 계획 기준</span></div>
    <div class="scroll"><table>
      <thead><tr><th>유형</th><th>거래/청산</th><th>승률</th><th>평균 수익</th><th>평균 손실</th><th>손익비</th><th>기대값</th><th>1R 도달률</th><th>반익절 후 본절</th></tr></thead>
      <tbody id="perf-type-performance"></tbody>
    </table></div>
  </div>
  <div class="card" style="margin-top:14px">
    <div class="card-title"><h2>전략 복기 요약</h2><span class="badge">30/60/120분 비교</span></div>
    <div class="grid">
      <div class="flow-metric"><span class="label">복기 판단</span><b id="replay-total">-</b><span id="replay-total-detail" class="muted">-</span></div>
      <div class="flow-metric"><span class="label">차단 적중</span><b id="replay-helpful">-</b><span class="muted">차단 뒤 하락</span></div>
      <div class="flow-metric"><span class="label">놓친 급등</span><b id="replay-missed">-</b><span class="muted">차단 뒤 상승</span></div>
      <div class="flow-metric"><span class="label">진입 성공/실패</span><b id="replay-entered">-</b><span class="muted">BUY 판단 후 흐름</span></div>
    </div>
    <div class="scroll" style="margin-top:12px"><table>
      <thead><tr><th>차단 사유</th><th>건수</th><th>차단 적중</th><th>놓친 급등</th><th>중립</th><th>평균 최고상승</th></tr></thead>
      <tbody id="replay-reasons"></tbody>
    </table></div>
  </div>
  <div class="card" style="margin-top:14px">
    <div class="card-title"><h2>오늘 매매 복기 리포트</h2><span id="trading-review-badge" class="badge">자동 집계</span></div>
    <div id="trading-review-summary" class="flow-top"></div>
    <div class="grid" style="margin-top:12px">
      <div>
        <div class="label">시간대별 손익</div>
        <div class="scroll"><table>
          <thead><tr><th>구간</th><th>BUY/SELL</th><th>실현손익</th><th>종목</th></tr></thead>
          <tbody id="trading-review-time"></tbody>
        </table></div>
      </div>
      <div>
        <div class="label">진입유형별 손익</div>
        <div class="scroll"><table>
          <thead><tr><th>유형</th><th>BUY/SELL</th><th>손익</th><th>손실/보호</th></tr></thead>
          <tbody id="trading-review-entry"></tbody>
        </table></div>
      </div>
    </div>
    <div class="grid" style="margin-top:12px">
      <div>
        <div class="label">반복매수 손익</div>
        <div class="scroll"><table>
          <thead><tr><th>종목</th><th>매수</th><th>손익</th><th>유형</th></tr></thead>
          <tbody id="trading-review-repeat"></tbody>
        </table></div>
      </div>
      <div>
        <div class="label">차단했으면 막았을 손실 추정</div>
        <div class="scroll"><table>
          <thead><tr><th>사유</th><th>손실건수</th><th>추정손실</th><th>종목</th></tr></thead>
          <tbody id="trading-review-preventable"></tbody>
        </table></div>
      </div>
    </div>
    <div style="margin-top:14px">
      <div class="label">전략 리메이크 복기</div>
      <div id="strategy-review-summary" class="flow-top"></div>
      <div style="margin-top:12px">
        <div class="label">모의운전 A/B 테스트</div>
        <div class="scroll"><table>
          <thead><tr><th>모드</th><th>진입 수</th><th>승률</th><th>평균 손익</th><th>놓친 랠리</th><th>불필요 진입</th><th>조기 청산</th><th>역할</th></tr></thead>
          <tbody id="strategy-review-ab"></tbody>
        </table></div>
      </div>
      <div class="grid" style="margin-top:12px">
        <div>
          <div class="label">오늘 A+ 후보</div>
          <div class="scroll"><table>
            <thead><tr><th>종목</th><th>진입</th><th>전략</th><th>최고상승</th><th>상태</th></tr></thead>
            <tbody id="strategy-review-aplus"></tbody>
          </table></div>
        </div>
        <div>
          <div class="label">3대 playbook 성과</div>
          <div class="scroll"><table>
            <thead><tr><th>playbook</th><th>BUY/SELL</th><th>손익</th><th>shadow</th></tr></thead>
            <tbody id="strategy-review-playbooks"></tbody>
          </table></div>
        </div>
      </div>
      <div class="grid" style="margin-top:12px">
        <div>
          <div class="label">legacy vs 새 라우터</div>
          <div class="scroll"><table>
            <thead><tr><th>구분</th><th>종목</th><th>전략</th><th>상태</th></tr></thead>
            <tbody id="strategy-review-legacy"></tbody>
          </table></div>
        </div>
        <div>
          <div class="label">차단 사유 TOP</div>
          <div class="scroll"><table>
            <thead><tr><th>사유</th><th>건수</th></tr></thead>
            <tbody id="strategy-review-blocks"></tbody>
          </table></div>
        </div>
      </div>
      <div style="margin-top:12px">
        <div class="label">legacy 제거 후보</div>
        <div class="scroll"><table>
          <thead><tr><th>경로</th><th>추천</th><th>old/new</th><th>손익</th><th>근거</th></tr></thead>
          <tbody id="strategy-review-legacy-retirement"></tbody>
        </table></div>
      </div>
      <div class="grid" style="margin-top:12px">
        <div>
          <div class="label">놓친 대장주</div>
          <div class="scroll"><table>
            <thead><tr><th>종목</th><th>전략</th><th>최고상승</th><th>차단</th></tr></thead>
            <tbody id="strategy-review-missed-leaders"></tbody>
          </table></div>
        </div>
        <div>
          <div class="label">너무 빨리 판 종목</div>
          <div class="scroll"><table>
            <thead><tr><th>종목</th><th>청산</th><th>실현</th><th>남긴 상승</th></tr></thead>
            <tbody id="strategy-review-sold-early"></tbody>
          </table></div>
        </div>
      </div>
      <div style="margin-top:12px">
        <div class="label">재진입했어야 했는데 못 한 종목</div>
        <div class="scroll"><table>
          <thead><tr><th>종목</th><th>전략</th><th>재진입 엔진</th><th>최고상승</th><th>차단</th></tr></thead>
          <tbody id="strategy-review-missed-reentry"></tbody>
        </table></div>
      </div>
    </div>
  </div>
  <div class="card" style="margin-top:14px">
    <div class="card-title"><h2>최근 복기 상세</h2><span id="replay-filter-badge" class="badge">최근 30건</span></div>
    <div class="filter-bar">
      <label>종목
        <select id="replay-filter-stock"><option value="">전체 종목</option></select>
      </label>
      <label>시작일<input id="replay-filter-start" type="date"></label>
      <label>종료일<input id="replay-filter-end" type="date"></label>
      <label>판단
        <select id="replay-filter-action"><option value="">전체 판단</option></select>
      </label>
      <label>차단 사유
        <select id="replay-filter-reason"><option value="">전체 사유</option></select>
      </label>
      <div class="filter-actions">
        <button class="start" onclick="applyReplayFilters()">필터 적용</button>
        <button class="secondary" onclick="resetReplayFilters()">초기화</button>
      </div>
    </div>
    <div id="replay-filter-status" class="muted" style="margin-bottom:10px">전체 복기 판단을 표시합니다.</div>
    <div class="scroll"><table>
      <thead><tr><th>시각</th><th>종목</th><th>판단</th><th>대표 결과</th><th>수급</th><th>근거 차트</th></tr></thead>
      <tbody id="replay-review-rows"></tbody>
    </table></div>
  </div>
  <div class="card" style="margin-top:14px">
    <div class="card-title"><h2>오늘 체결 내역</h2><span class="badge">최근 30건</span></div>
    <div class="scroll"><table>
      <thead><tr><th>체결시각</th><th>종목</th><th>구분</th><th>수량</th><th>체결가</th><th>수수료·세금</th><th>실현손익</th><th>사유</th></tr></thead>
      <tbody id="perf-fills"></tbody>
    </table></div>
  </div>
</section>

<section id="theme-trends-panel" class="panel">
  <div class="notice">최근 30일 저장된 테마 저널과 대장주 점수를 묶어 반복 주도 테마를 계산합니다. 반복 등장, strong 기록, leader/second 배출, 평균 대장주 점수가 높을수록 신뢰도가 높습니다.</div>
  <div class="grid" style="margin-top:14px">
    <div class="card"><div class="label">반복 테마 수</div><div id="theme-trend-count" class="metric">-</div><div class="muted">최근 30일 기준</div></div>
    <div class="card"><div class="label">최상위 신뢰 테마</div><div id="theme-trend-top" class="metric">-</div><div id="theme-trend-top-detail" class="muted">-</div></div>
    <div class="card"><div class="label">강한 테마 기록</div><div id="theme-trend-strong" class="metric">-</div><div class="muted">수동 strong + 대장주 배출 참고</div></div>
  </div>
  <div class="card" style="margin-top:14px">
    <div class="card-title"><h2>최근 30일 주도 테마 신뢰도</h2><span class="badge">후보 선정 보조 근거</span></div>
    <div class="scroll"><table>
      <thead><tr><th>신뢰</th><th>테마</th><th>점수</th><th>등장일</th><th>저널/점수일</th><th>강도</th><th>대장/2등</th><th>평균 대장주 점수</th><th>최근일</th></tr></thead>
      <tbody id="theme-trends"></tbody>
    </table></div>
  </div>
</section>

<section id="leader-panel" class="panel">
  <div class="notice">대장주 엔진 화면입니다. 시장 순위·테마·지속성·일봉 상태를 강하게 점수화하고 후보 선정 총점에 우선 가산점으로 반영합니다. 대장주가 아니어도 차트가 좋으면 후보로 남습니다.</div>
  <div class="card" style="margin-top:14px">
    <div class="card-title"><h2>오늘 대장주 후보 TOP 20</h2><span class="badge">거래대금 필수</span></div>
    <div id="leader-summary" class="muted"></div>
    <div class="scroll"><table>
      <thead><tr><th>역할</th><th>종목</th><th>총점</th><th>거래</th><th>순위</th><th>테마</th><th>지속</th><th>일봉</th><th>위험</th><th>차단·근거</th></tr></thead>
      <tbody id="leader-scores"></tbody>
    </table></div>
  </div>
  <div class="card" style="margin-top:14px">
    <div class="card-title"><h2>대장주 학습 리포트</h2><span class="badge">성과 학습</span></div>
    <div id="leader-learning-summary" class="flow-top"></div>
    <div class="grid" style="margin-top:12px">
      <div>
        <div class="label">테마별 적중률</div>
        <div class="scroll"><table>
          <thead><tr><th>테마</th><th>후보</th><th>적중</th><th>적중률</th><th>평균 최고상승</th></tr></thead>
          <tbody id="leader-learning-themes"></tbody>
        </table></div>
      </div>
      <div>
        <div class="label">점수 구성요소별 평균 성과</div>
        <div class="scroll"><table>
          <thead><tr><th>구성요소</th><th>후보</th><th>적중률</th><th>평균 성과</th></tr></thead>
          <tbody id="leader-learning-components"></tbody>
        </table></div>
      </div>
    </div>
    <div class="scroll" style="margin-top:12px"><table>
      <thead><tr><th>blocked 사유</th><th>후보</th><th>차단 적중</th><th>놓친 급등</th><th>평균 성과</th></tr></thead>
      <tbody id="leader-learning-blocked"></tbody>
    </table></div>
  </div>
  <div class="grid" style="margin-top:14px">
    <div class="card">
      <div class="card-title"><h2>날짜별 강한 테마</h2><span class="badge">최근 30일</span></div>
      <div class="scroll"><table>
        <thead><tr><th>날짜</th><th>테마</th><th>강도</th><th>구성</th><th>메모</th></tr></thead>
        <tbody id="theme-journals"></tbody>
      </table></div>
    </div>
    <div class="card">
      <div class="card-title"><h2>시장 순위 수집 상태</h2><span class="badge">0198·0183·0187·0181</span></div>
      <div class="scroll"><table>
        <thead><tr><th>유형</th><th>최근 관측</th><th>대표 종목</th><th>거래대금</th></tr></thead>
        <tbody id="market-rank-status"></tbody>
      </table></div>
    </div>
  </div>
</section>

<section id="live-panel" class="panel">
  <div class="notice danger-notice">조회 전용 화면입니다. 현재 프로그램에는 실제 매수·매도·정정·취소 기능이 없습니다.</div>
  <div class="grid" style="margin-top:14px">
    <div class="card"><div class="label">마지막 계좌 동기화</div><div id="real-time" class="value">이력 없음</div></div>
    <div class="card"><div class="label">예수금</div><div id="real-deposit" class="metric">-</div></div>
    <div class="card"><div class="label">추정 자산</div><div id="real-assets" class="metric">-</div></div>
    <div class="card"><div class="label">평가 손익</div><div id="real-pnl" class="metric">-</div></div>
  </div>
  <div class="card" style="margin-top:14px">
    <div class="card-title"><h2>실전 보유 종목 스냅샷</h2><span class="badge">조회 전용</span></div>
    <div class="scroll"><table>
      <thead><tr><th>종목</th><th>수량</th><th>현재가</th><th>매입가</th><th>평가금액</th><th>손익</th></tr></thead>
      <tbody id="real-positions"></tbody>
    </table></div>
  </div>
  <div class="card" style="margin-top:14px">
    <div class="card-title"><h2>최근 주문·체결 조회</h2><span class="badge">최근 10건</span></div>
    <div class="scroll"><table>
      <thead><tr><th>시각</th><th>종목</th><th>구분</th><th>주문</th><th>체결</th><th>상태</th></tr></thead>
      <tbody id="real-orders"></tbody>
    </table></div>
  </div>
</section>

<div class="card" style="margin-top:14px">
  <div class="card-title"><h2>최근 운영 이벤트</h2><span class="badge">공통</span></div>
  <div class="scroll"><table><thead><tr><th>시각</th><th>모듈</th><th>상태</th><th>내용</th></tr></thead><tbody id="events"></tbody></table></div>
</div>
</main>
<script>
const cls = (ok, warn=false) => ok ? 'value ok' : (warn ? 'value warn' : 'value bad');
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({
  '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
}[c]));
const money = value => value == null ? '-' : `${Number(value).toLocaleString()}원`;
const pct = value => value == null ? '-' : `${(Number(value) * 100).toFixed(1)}%`;
let replayFiltersApplied = false;
let latestReplayReview = null;
let replayFilterOptionsLoaded = false;
let replayLoading = false;
function showTab(name) {
  for (const value of ['paper', 'performance', 'theme-trends', 'leader', 'live']) {
    document.getElementById(`${value}-panel`).classList.toggle('active', value === name);
    document.getElementById(`${value}-tab`).classList.toggle('active', value === name);
  }
  localStorage.setItem('dashboard-tab', name);
}
function pnlClass(value) {
  const number = Number(value || 0);
  if (number > 0) return 'pnl-positive';
  if (number < 0) return 'pnl-negative';
  return 'pnl-flat';
}
function signedMoney(value) {
  if (value == null) return '-';
  const number = Number(value);
  const prefix = number > 0 ? '+' : '';
  return `${prefix}${number.toLocaleString()}원`;
}
function outcomeLabel(value) {
  return {
    entered_then_rallied: '진입 후 상승',
    entered_then_dropped: '진입 후 하락',
    entered_flat: '진입 후 보합',
    missed_rally_after_block: '차단 후 급등',
    block_was_helpful: '차단 적중',
    block_neutral: '차단 중립'
  }[value] || value || '-';
}
function renderReplayChart(replay, plan) {
  if (!replay || !replay.candles || !replay.candles.length) return '';
  const candles = replay.candles.filter(c => c.candle_type === 'minute' && c.close_price != null).slice(-30);
  if (!candles.length) return '';
  const prices = [];
  candles.forEach(c => {
    ['open_price', 'high_price', 'low_price', 'close_price'].forEach(k => {
      if (c[k] != null) prices.push(Number(c[k]));
    });
  });
  [plan?.entry_price, plan?.stop_loss_price, plan?.first_take_profit_price, plan?.setup?.second_take_profit_price].forEach(v => {
    if (v != null) prices.push(Number(v));
  });
  const minP = Math.min(...prices);
  const maxP = Math.max(...prices);
  const pad = Math.max(1, (maxP - minP) * 0.08);
  const lo = minP - pad;
  const hi = maxP + pad;
  const w = 500, h = 140, top = 12, bottom = 18;
  const chartH = h - top - bottom;
  const xStep = w / Math.max(candles.length, 1);
  const y = price => top + (hi - Number(price)) / Math.max(hi - lo, 1) * chartH;
  const bars = candles.map((c, i) => {
    const x = i * xStep + xStep / 2;
    const open = y(c.open_price), close = y(c.close_price);
    const high = y(c.high_price), low = y(c.low_price);
    const up = Number(c.close_price) >= Number(c.open_price);
    const color = up ? '#4ade80' : '#fb7185';
    const bodyY = Math.min(open, close);
    const bodyH = Math.max(2, Math.abs(open - close));
    const bw = Math.max(3, xStep * 0.55);
    return `<line x1="${x.toFixed(1)}" y1="${high.toFixed(1)}" x2="${x.toFixed(1)}" y2="${low.toFixed(1)}" stroke="${color}" stroke-width="1"/><rect x="${(x-bw/2).toFixed(1)}" y="${bodyY.toFixed(1)}" width="${bw.toFixed(1)}" height="${bodyH.toFixed(1)}" fill="${color}" opacity="0.85"/>`;
  }).join('');
  const lines = [
    ['진입', plan?.entry_price, '#60a5fa'],
    ['손절', plan?.stop_loss_price, '#fb7185'],
    ['1차', plan?.first_take_profit_price, '#fbbf24'],
    ['2차', plan?.setup?.second_take_profit_price, '#a78bfa']
  ].filter(x => x[1] != null).map(([label, price, color]) => {
    const yy = y(price);
    return `<line x1="0" y1="${yy.toFixed(1)}" x2="${w}" y2="${yy.toFixed(1)}" stroke="${color}" stroke-dasharray="4 4" opacity="0.9"/><text x="4" y="${Math.max(10, yy-3).toFixed(1)}" fill="${color}" font-size="10">${esc(label)} ${Number(price).toLocaleString()}</text>`;
  }).join('');
  const outcomes = (replay.forward_outcomes || []).map(o => {
    const clsName = o.actual_outcome === 'missed_rally_after_block' || o.actual_outcome === 'entered_then_dropped' ? 'bad' : (o.actual_outcome === 'block_was_helpful' || o.actual_outcome === 'entered_then_rallied' ? 'ok' : 'warn');
    return `<span class="chip ${clsName}" title="하락 ${pct(o.max_down_rate)} · 종가 ${pct(o.close_rate)}">${Number(o.horizon_minutes)}분 ${outcomeLabel(o.actual_outcome)} · 최고 ${pct(o.max_up_rate)}</span>`;
  }).join('');
  return `<div class="replay-chart"><svg viewBox="0 0 ${w} ${h}" role="img" aria-label="복기 캔들 차트">${bars}${lines}</svg>${outcomes ? `<div class="replay-outcomes">${outcomes}</div>` : ''}</div>`;
}
function renderPerformance(performance) {
  if (!performance) return;
  const summary = performance.summary || {};
  const last = performance.last_closed_trade;
  const lastResult = document.getElementById('perf-last-result');
  if (last) {
    lastResult.textContent = last.realized_pnl > 0 ? '이익 종료' : (last.realized_pnl < 0 ? '손실 종료' : '본전 종료');
    lastResult.className = `metric ${pnlClass(last.realized_pnl)}`;
    document.getElementById('perf-last-detail').textContent =
      `${last.stock_name || last.stock_code} · ${signedMoney(last.realized_pnl)} · ${last.filled_at}`;
  } else {
    lastResult.textContent = '청산 없음';
    lastResult.className = 'metric pnl-flat';
    document.getElementById('perf-last-detail').textContent = '오늘 매도 체결이 아직 없습니다.';
  }
  const realized = document.getElementById('perf-realized');
  realized.textContent = signedMoney(summary.realized_pnl);
  realized.className = `metric ${pnlClass(summary.realized_pnl)}`;
  document.getElementById('perf-realized-detail').textContent =
    `이익 ${summary.winning_trades || 0}건 · 손실 ${summary.losing_trades || 0}건 · 승률 ${summary.closed_trades ? ((summary.winning_trades / summary.closed_trades) * 100).toFixed(0) : 0}%`;
  const unrealized = document.getElementById('perf-unrealized');
  unrealized.textContent = signedMoney(summary.unrealized_pnl);
  unrealized.className = `metric ${pnlClass(summary.unrealized_pnl)}`;
  document.getElementById('perf-unrealized-detail').textContent =
    `보유 ${summary.position_count || 0}종목 · 평가 ${money(summary.market_value || 0)}`;
  const total = document.getElementById('perf-total');
  total.textContent = signedMoney(summary.total_pnl);
  total.className = `metric ${pnlClass(summary.total_pnl)}`;
  document.getElementById('perf-total-detail').textContent =
    `오늘 체결 ${summary.filled_orders || 0}건 · 매수 ${summary.buy_fills || 0} / 매도 ${summary.sell_fills || 0}`;
  document.getElementById('perf-accounts').innerHTML = performance.accounts.length ?
    performance.accounts.map(a => {
      const equityDelta = Number(a.total_equity || 0) - Number(a.initial_cash || 0);
      return `<div class="account-row"><b>${esc(a.name)}</b><div class="metric ${pnlClass(equityDelta)}">${signedMoney(equityDelta)}</div><span class="muted">총자산 ${money(a.total_equity)} · 현금 ${money(a.cash_balance)} · 누적 실현 ${signedMoney(a.realized_pnl)}</span></div>`;
    }).join('') :
    '<p class="muted">생성된 모의계좌가 없습니다.</p>';
  document.getElementById('perf-positions').innerHTML = performance.positions.length ?
    performance.positions.map(p => {
      const rate = p.return_rate == null ? null : Number(p.return_rate) * 100;
      return `<tr><td>${esc(p.stock_name || p.stock_code)}</td><td>${Number(p.quantity).toLocaleString()}</td><td>${money(p.average_price)}</td><td>${money(p.current_price)}</td><td class="${pnlClass(p.unrealized_pnl)}">${signedMoney(p.unrealized_pnl)}</td><td class="${pnlClass(p.unrealized_pnl)}">${rate == null ? '-' : `${rate >= 0 ? '+' : ''}${rate.toFixed(2)}%`}</td></tr>`;
    }).join('') :
    '<tr><td colspan="6" class="muted">현재 보유 중인 모의 종목이 없습니다.</td></tr>';
  const pct = value => value == null ? '-' : `${(Number(value) * 100).toFixed(0)}%`;
  const ratio = value => value == null ? '-' : `${Number(value).toFixed(2)}배`;
  const typePerformance = performance.type_performance || [];
  document.getElementById('perf-type-performance').innerHTML = typePerformance.length ?
    typePerformance.map(row => `<tr><td>${esc(row.entry_type)}</td><td>${Number(row.trade_count).toLocaleString()} / ${Number(row.closed_trades).toLocaleString()}</td><td>${pct(row.win_rate)}</td><td class="pnl-positive">${signedMoney(row.average_profit)}</td><td class="pnl-negative">${signedMoney(row.average_loss)}</td><td>${ratio(row.profit_loss_ratio)}</td><td class="${pnlClass(row.expectancy)}">${signedMoney(row.expectancy)}</td><td>${pct(row.one_r_hit_rate)}</td><td>${row.partial_breakeven_count ? `${pct(row.partial_breakeven_success_rate)} (${row.partial_breakeven_success}/${row.partial_breakeven_count})` : '-'}</td></tr>`).join('') :
    '<tr><td colspan="9" class="muted">아직 매매 계획 기반 성과 데이터가 없습니다.</td></tr>';
  document.getElementById('perf-fills').innerHTML = performance.today_fills.length ?
    performance.today_fills.map(fill => `<tr><td>${esc(fill.filled_at)}</td><td>${esc(fill.stock_name || fill.stock_code)}</td><td>${esc(fill.side)}</td><td>${Number(fill.quantity).toLocaleString()}</td><td>${money(fill.fill_price)}</td><td>${money((fill.commission || 0) + (fill.tax || 0))}</td><td class="${pnlClass(fill.realized_pnl)}">${signedMoney(fill.realized_pnl)}</td><td>${esc(fill.display_reason || fill.reason || '-')}</td></tr>`).join('') :
    '<tr><td colspan="8" class="muted">오늘 체결된 모의 주문이 없습니다.</td></tr>';
}
function renderReplayReview(review) {
  if (!review) return;
  latestReplayReview = review;
  populateReplayFilterOptions(review.filters || {});
  const summary = review.summary || {};
  document.getElementById('replay-total').textContent = Number(summary.total || 0).toLocaleString();
  document.getElementById('replay-total-detail').textContent =
    `BUY ${summary.buy_decisions || 0} · 차단 ${summary.blocked_decisions || 0} · 평균 최고 ${pct(summary.average_max_up_rate)}`;
  document.getElementById('replay-helpful').textContent = Number(summary.block_was_helpful || 0).toLocaleString();
  document.getElementById('replay-missed').textContent = Number(summary.missed_rally_after_block || 0).toLocaleString();
  document.getElementById('replay-entered').textContent =
    `${summary.entered_then_rallied || 0}/${summary.entered_then_dropped || 0}`;
  const reasons = review.by_block_reason || [];
  document.getElementById('replay-reasons').innerHTML = reasons.length ?
    reasons.map(row => `<tr><td>${esc(row.reason)}</td><td>${Number(row.count).toLocaleString()}</td><td class="ok">${Number(row.block_was_helpful || 0).toLocaleString()}</td><td class="bad">${Number(row.missed_rally_after_block || 0).toLocaleString()}</td><td>${Number(row.block_neutral || 0).toLocaleString()}</td><td>${pct(row.average_max_up_rate)}</td></tr>`).join('') :
    '<tr><td colspan="6" class="muted">집계할 차단 사유가 없습니다.</td></tr>';
  const rows = review.rows || [];
  const applied = review.applied_filters || {};
  const activeCount = Object.values(applied).filter(Boolean).length;
  const badge = document.getElementById('replay-filter-badge');
  badge.textContent = activeCount ?
    `필터 ${activeCount}개 · ${Number(summary.total || 0).toLocaleString()}건` :
    `최근 ${Number(summary.total || 0).toLocaleString()}건`;
  badge.className = `badge ${activeCount ? 'ok' : ''}`;
  document.getElementById('replay-filter-status').textContent = activeCount ?
    `조건 적용 · 종목 ${applied.stock_code || '전체'} · 기간 ${applied.start_date || '전체'} ~ ${applied.end_date || '전체'} · 판단 ${applied.action || '전체'} · 차단 ${applied.block_reason || '전체'}` :
    '전체 복기 판단을 표시합니다.';
  document.getElementById('replay-review-rows').innerHTML = rows.length ?
    rows.map(row => {
      const outcome = row.primary_outcome || {};
      const supply = row.supply_score || {};
      const leader = row.leader_score || {};
      const outcomeClass = ['missed_rally_after_block', 'entered_then_dropped'].includes(outcome.actual_outcome) ? 'bad' :
        (['block_was_helpful', 'entered_then_rallied'].includes(outcome.actual_outcome) ? 'ok' : 'warn');
      const plan = row.trade_plan || {entry_price: outcome.details?.entry_price};
      const chart = renderReplayChart(row, plan);
      const blockText = (row.block_reasons || []).join(' · ');
      const supplyText = [
        supply.total_score != null ? `${Number(supply.total_score).toFixed(1)}점` : '-',
        supply.blocked ? `차단 ${supply.blocked_reason || ''}` : '',
        leader.role ? `대장 ${leader.role}` : ''
      ].filter(Boolean).join('<br>');
      const outcomeText = outcome.actual_outcome ?
        `${outcomeLabel(outcome.actual_outcome)}<br><span class="muted">${outcome.horizon_minutes}분 · 최고 ${pct(outcome.max_up_rate)} · 최저 ${pct(outcome.max_down_rate)}</span>` :
        '<span class="muted">성과 대기</span>';
      return `<tr><td>${esc(row.evaluated_at)}</td><td><b>${esc(row.stock_code)}</b></td><td>${esc(row.action)}<br><span class="muted">${esc(blockText || row.reason || '-')}</span></td><td class="${outcomeClass}">${outcomeText}</td><td>${supplyText}</td><td>${chart || '<span class="muted">캔들 없음</span>'}</td></tr>`;
    }).join('') :
    '<tr><td colspan="6" class="muted">저장된 전략 복기 스냅샷이 없습니다.</td></tr>';
}
function populateReplayFilterOptions(filters) {
  const stock = document.getElementById('replay-filter-stock');
  const action = document.getElementById('replay-filter-action');
  const reason = document.getElementById('replay-filter-reason');
  const current = {
    stock: stock.value,
    action: action.value,
    reason: reason.value
  };
  stock.innerHTML = '<option value="">전체 종목</option>' +
    (filters.stock_codes || []).map(value => `<option value="${esc(value)}">${esc(value)}</option>`).join('');
  action.innerHTML = '<option value="">전체 판단</option>' +
    (filters.actions || []).map(value => `<option value="${esc(value)}">${esc(value)}</option>`).join('');
  reason.innerHTML = '<option value="">전체 사유</option>' +
    (filters.block_reasons || []).map(value => `<option value="${esc(value)}">${esc(value)}</option>`).join('');
  stock.value = current.stock;
  action.value = current.action;
  reason.value = current.reason;
  if (!replayFilterOptionsLoaded) {
    document.getElementById('replay-filter-start').min = filters.min_date || '';
    document.getElementById('replay-filter-start').max = filters.max_date || '';
    document.getElementById('replay-filter-end').min = filters.min_date || '';
    document.getElementById('replay-filter-end').max = filters.max_date || '';
    replayFilterOptionsLoaded = true;
  }
}
async function applyReplayFilters() {
  if (replayLoading) return;
  replayLoading = true;
  const params = new URLSearchParams();
  const values = {
    stock_code: document.getElementById('replay-filter-stock').value,
    start_date: document.getElementById('replay-filter-start').value,
    end_date: document.getElementById('replay-filter-end').value,
    action: document.getElementById('replay-filter-action').value,
    block_reason: document.getElementById('replay-filter-reason').value
  };
  Object.entries(values).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });
  params.set('limit', Object.values(values).some(Boolean) ? '50' : '8');
  const status = document.getElementById('replay-filter-status');
  status.textContent = '복기 데이터를 조회하고 있습니다.';
  try {
    const response = await fetch(`/api/replay-review?${params.toString()}`, {cache:'no-store'});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || '복기 조회 실패');
    replayFiltersApplied = Object.values(values).some(Boolean);
    renderReplayReview(payload);
  } catch (error) {
    status.textContent = `복기 조회 실패: ${error.message || error}`;
  } finally {
    replayLoading = false;
  }
}
function resetReplayFilters() {
  document.getElementById('replay-filter-stock').value = '';
  document.getElementById('replay-filter-start').value = '';
  document.getElementById('replay-filter-end').value = '';
  document.getElementById('replay-filter-action').value = '';
  document.getElementById('replay-filter-reason').value = '';
  replayFiltersApplied = false;
  if (latestReplayReview) {
    applyReplayFilters();
  }
}
function renderTradingReview(report) {
  if (!report) return;
  const summary = report.summary || {};
  const preventable = report.preventable_loss_estimate || {};
  const missingSupply = report.missing_supply_performance || {};
  const forceEntry = report.force_entry_performance || {};
  const blocked = report.blocked_forward_review || {};
  const lossStops = Number(summary.loss_stop_count || 0);
  const profitStops = Number(summary.profit_protect_stop_count || 0);
  const trailingLocks = Number(summary.trailing_profit_lock_count || 0);
  const badge = document.getElementById('trading-review-badge');
  badge.textContent = report.report_date || 'today';
  badge.className = `badge ${Number(summary.realized_pnl || 0) < 0 ? 'warn' : 'ok'}`;
  document.getElementById('trading-review-summary').innerHTML = `
    <div class="flow-metric"><span class="label">실현손익</span><b class="${pnlClass(summary.realized_pnl)}">${signedMoney(summary.realized_pnl || 0)}</b><span class="muted">BUY ${Number(summary.buy_count || 0).toLocaleString()} / SELL ${Number(summary.sell_count || 0).toLocaleString()}</span></div>
    <div class="flow-metric"><span class="label">손실손절</span><b class="${lossStops ? 'bad' : 'ok'}">${lossStops.toLocaleString()}</b><span class="muted">전체 stop ${Number(summary.stop_loss_count || 0).toLocaleString()}회</span></div>
    <div class="flow-metric"><span class="label">수익보호</span><b class="${profitStops || trailingLocks ? 'ok' : 'muted'}">${(profitStops + trailingLocks).toLocaleString()}</b><span class="muted">손익 ${signedMoney((summary.profit_protect_stop_realized_pnl || 0) + (summary.trailing_profit_lock_realized_pnl || 0))}</span></div>
    <div class="flow-metric"><span class="label">수급 없음</span><b class="${Number(missingSupply.realized_pnl || 0) < 0 ? 'bad' : 'ok'}">${signedMoney(missingSupply.realized_pnl || 0)}</b><span class="muted">${Number(missingSupply.buy_count || 0).toLocaleString()} BUY</span></div>
    <div class="flow-metric"><span class="label">force entry</span><b class="${Number(forceEntry.realized_pnl || 0) < 0 ? 'bad' : 'ok'}">${signedMoney(forceEntry.realized_pnl || 0)}</b><span class="muted">${Number(forceEntry.buy_count || 0).toLocaleString()} BUY</span></div>
    <div class="flow-metric"><span class="label">차단 추정손실</span><b class="${preventable.total_preventable_loss ? 'warn' : 'ok'}">${money(preventable.total_preventable_loss || 0)}</b><span class="muted">${Number(preventable.losing_sell_count || 0).toLocaleString()}건</span></div>
    <div class="flow-metric"><span class="label">차단 후 급등</span><b class="${blocked.missed_spike_count ? 'bad' : 'ok'}">${Number(blocked.missed_spike_count || 0).toLocaleString()}</b><span class="muted">평가 ${Number(blocked.evaluated_count || 0).toLocaleString()}건</span></div>
  `;
  const timeRows = report.time_buckets || [];
  document.getElementById('trading-review-time').innerHTML = timeRows.length ?
    timeRows.slice(0, 12).map(row => `<tr><td>${esc(row.bucket)}</td><td>${Number(row.buy_count || 0).toLocaleString()} / ${Number(row.sell_count || 0).toLocaleString()}</td><td class="${pnlClass(row.realized_pnl)}">${signedMoney(row.realized_pnl || 0)}</td><td>${(row.stock_codes || []).map(esc).join(', ')}</td></tr>`).join('') :
    '<tr><td colspan="4" class="muted">오늘 시간대별 체결 데이터가 없습니다.</td></tr>';
  const entryRows = report.entry_type_performance || [];
  document.getElementById('trading-review-entry').innerHTML = entryRows.length ?
    entryRows.slice(0, 12).map(row => `<tr><td>${esc(row.entry_type)}</td><td>${Number(row.buy_count || 0).toLocaleString()} / ${Number(row.sell_count || 0).toLocaleString()}</td><td class="${pnlClass(row.realized_pnl)}">${signedMoney(row.realized_pnl || 0)}</td><td><span class="${Number(row.loss_stop_count || 0) ? 'bad' : 'muted'}">${Number(row.loss_stop_count || 0).toLocaleString()}</span> / <span class="${Number(row.profit_protect_stop_count || 0) + Number(row.trailing_profit_lock_count || 0) ? 'ok' : 'muted'}">${(Number(row.profit_protect_stop_count || 0) + Number(row.trailing_profit_lock_count || 0)).toLocaleString()}</span></td></tr>`).join('') :
    '<tr><td colspan="4" class="muted">진입유형별 복기 데이터가 없습니다.</td></tr>';
  const repeatRows = report.stock_repeat_buys || [];
  document.getElementById('trading-review-repeat').innerHTML = repeatRows.length ?
    repeatRows.slice(0, 12).map(row => `<tr><td>${esc(row.stock_code)}</td><td>${Number(row.buy_count || 0).toLocaleString()}</td><td class="${pnlClass(row.realized_pnl)}">${signedMoney(row.realized_pnl || 0)}</td><td>${(row.entry_types || []).map(esc).join(', ')}</td></tr>`).join('') :
    '<tr><td colspan="4" class="muted">반복매수 복기 대상이 없습니다.</td></tr>';
  const preventableRows = preventable.by_reason || [];
  document.getElementById('trading-review-preventable').innerHTML = preventableRows.length ?
    preventableRows.slice(0, 12).map(row => `<tr><td>${esc(row.reason)}</td><td>${Number(row.losing_sell_count || 0).toLocaleString()}</td><td class="bad">${money(row.preventable_loss || 0)}</td><td>${(row.stock_codes || []).map(esc).join(', ')}</td></tr>`).join('') :
    '<tr><td colspan="4" class="muted">차단했으면 막았을 손실 추정값이 없습니다.</td></tr>';
  renderStrategyDashboardReview(report.strategy_dashboard_review || {});
}
function renderStrategyDashboardReview(review) {
  const summary = review.summary || {};
  const setHtml = (id, html) => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = html;
  };
  setHtml('strategy-review-summary', `
    <div class="flow-metric"><span class="label">A+ 후보</span><b>${Number(summary.a_plus_entered_count || 0).toLocaleString()} / ${Number(summary.a_plus_candidate_count || 0).toLocaleString()}</b><span class="muted">진입 / 전체</span></div>
    <div class="flow-metric"><span class="label">3대 playbook BUY</span><b>${Number(summary.core_playbook_buy_count || 0).toLocaleString()}</b><span class="muted">실제 체결 기준</span></div>
    <div class="flow-metric"><span class="label">legacy vs new</span><b>${Number(summary.legacy_old_logic_buy_count || 0).toLocaleString()} / ${Number(summary.new_router_buy_count || 0).toLocaleString()}</b><span class="muted">oldBUY / newBUY</span></div>
    <div class="flow-metric"><span class="label">놓친 대장주</span><b class="${Number(summary.missed_leader_count || 0) ? 'bad' : 'ok'}">${Number(summary.missed_leader_count || 0).toLocaleString()}</b><span class="muted">${esc(summary.top_block_reason || '-')}</span></div>
    <div class="flow-metric"><span class="label">조기 청산</span><b class="${Number(summary.sold_too_early_count || 0) ? 'warn' : 'ok'}">${Number(summary.sold_too_early_count || 0).toLocaleString()}</b><span class="muted">수익 보호 후 추가 상승</span></div>
    <div class="flow-metric"><span class="label">재진입 누락</span><b class="${Number(summary.missed_reentry_count || 0) ? 'bad' : 'ok'}">${Number(summary.missed_reentry_count || 0).toLocaleString()}</b><span class="muted">새 셋업 후보</span></div>
  `);
  const ab = review.ab_test || {};
  const abModes = ab.modes || [];
  setHtml('strategy-review-ab', abModes.length ?
    abModes.map(row => {
      const role = Number(row.selected_mode_count || 0) ? '실제 주문' : 'shadow';
      const avg = row.actual_sell_count ? signedMoney(row.actual_realized_pnl || 0) : pct(row.estimated_average_rate);
      return `<tr><td><b>${esc(row.label || row.mode)}</b><br><span class="muted">${esc(row.mode)}</span></td><td>${Number(row.would_enter_count || 0).toLocaleString()}<br><span class="muted">실제 ${Number(row.actual_entry_count || 0).toLocaleString()}</span></td><td>${pct(row.win_rate)}<br><span class="muted">실제 ${pct(row.actual_win_rate)}</span></td><td class="${pnlClass(row.actual_realized_pnl)}">${avg}</td><td class="${Number(row.missed_rally_count || 0) ? 'bad' : 'ok'}">${Number(row.missed_rally_count || 0).toLocaleString()}</td><td class="${Number(row.unnecessary_entry_count || 0) ? 'warn' : 'ok'}">${Number(row.unnecessary_entry_count || 0).toLocaleString()}</td><td class="${Number(row.early_exit_count || 0) ? 'warn' : 'ok'}">${Number(row.early_exit_count || 0).toLocaleString()}</td><td>${role}</td></tr>`;
    }).join('') :
    '<tr><td colspan="8" class="muted">A/B 비교 데이터가 아직 없습니다.</td></tr>');
  const aPlus = review.a_plus_candidates || [];
  setHtml('strategy-review-aplus', aPlus.length ?
    aPlus.slice(0, 12).map(row => `<tr><td><b>${esc(row.stock_name || row.stock_code)}</b><br><span class="muted">${esc(row.stock_code || '')}</span></td><td><span class="chip ${row.entered ? 'ok' : 'warn'}">${row.entered ? '진입' : '미진입'}</span></td><td>${esc(row.playbook || '-')}<br><span class="muted">${esc(row.entry_type || '-')}</span></td><td>${pct(row.max_up_rate)}</td><td>${esc(row.guard_status || row.router_status || row.outcome || '-')}</td></tr>`).join('') :
    '<tr><td colspan="5" class="muted">오늘 A+ 후보 복기 데이터가 없습니다.</td></tr>');
  const playbooks = review.playbook_performance || [];
  setHtml('strategy-review-playbooks', playbooks.length ?
    playbooks.map(row => `<tr><td><b>${esc(row.playbook || row.name)}</b></td><td>${Number(row.buy_count || 0).toLocaleString()} / ${Number(row.sell_count || 0).toLocaleString()}</td><td class="${pnlClass(row.realized_pnl)}">${signedMoney(row.realized_pnl || 0)}</td><td>${Number(row.shadow_only_count || 0).toLocaleString()}</td></tr>`).join('') :
    '<tr><td colspan="4" class="muted">3대 playbook 복기 데이터가 없습니다.</td></tr>');
  const legacy = review.legacy_vs_new_router || {};
  const legacyRows = [
    ...(legacy.legacy_old_logic_buys || []).slice(0, 6).map(row => ({...row, group: 'legacy oldBUY', status: row.legacy_route || row.router_status || '-'})),
    ...(legacy.new_router_buys || []).slice(0, 6).map(row => ({...row, group: 'new router BUY', status: row.order_effect || row.router_status || '-'})),
  ];
  setHtml('strategy-review-legacy', legacyRows.length ?
    legacyRows.map(row => `<tr><td>${esc(row.group)}</td><td><b>${esc(row.stock_name || row.stock_code)}</b><br><span class="muted">${esc(row.stock_code || '')}</span></td><td>${esc(row.playbook || row.entry_type || '-')}</td><td>${esc(row.status || '-')}</td></tr>`).join('') :
    '<tr><td colspan="4" class="muted">legacy/new 라우터 비교 데이터가 없습니다.</td></tr>');
  const blocks = review.top_block_reasons || [];
  setHtml('strategy-review-blocks', blocks.length ?
    blocks.slice(0, 12).map(row => `<tr><td>${esc(row.key)}</td><td>${Number(row.count || 0).toLocaleString()}</td></tr>`).join('') :
    '<tr><td colspan="2" class="muted">차단 사유 집계가 없습니다.</td></tr>');
  const retirement = review.legacy_retirement_review || {};
  const retirementLabels = retirement.recommendation_labels || {};
  const retirementRows = retirement.routes || [];
  setHtml('strategy-review-legacy-retirement', retirementRows.length ?
    retirementRows.slice(0, 12).map(row => {
      const rec = row.recommendation || 'keep_observe';
      const recClass = rec === 'remove' ? 'bad' : (rec === 'permanent_shadow' ? 'warn' : 'ok');
      const evidence = [...(row.evidence || []), ...(row.warnings || [])].slice(0, 3).join(', ');
      return `<tr><td><b>${esc(row.legacy_route || '-')}</b><br><span class="muted">${esc((row.entry_types || []).join(', ') || '-')}</span></td><td><span class="chip ${recClass}">${esc(retirementLabels[rec] || rec)}</span><br><span class="muted">score ${Number(row.retirement_score || 0).toLocaleString()}</span></td><td>${Number(row.old_logic_buy_count || 0).toLocaleString()} / ${Number(row.new_logic_buy_count || 0).toLocaleString()}<br><span class="muted">shadow ${Number(row.shadow_only_count || 0).toLocaleString()}</span></td><td class="${pnlClass(row.realized_pnl)}">${signedMoney(row.realized_pnl || 0)}<br><span class="muted">missed ${Number(row.missed_rally_count || 0).toLocaleString()}</span></td><td>${esc(evidence || '-')}</td></tr>`;
    }).join('') :
    '<tr><td colspan="5" class="muted">legacy 제거 판정 데이터가 아직 없습니다.</td></tr>');
  const missed = review.missed_leaders || [];
  setHtml('strategy-review-missed-leaders', missed.length ?
    missed.slice(0, 12).map(row => `<tr><td><b>${esc(row.stock_name || row.stock_code)}</b><br><span class="muted">${esc(row.stock_code || '')}</span></td><td>${esc(row.playbook || row.entry_type || '-')}</td><td class="bad">${pct(row.max_up_rate)}</td><td>${esc((row.block_reasons || [])[0] || row.guard_status || row.router_status || '-')}</td></tr>`).join('') :
    '<tr><td colspan="4" class="muted">놓친 대장주 후보가 없습니다.</td></tr>');
  const soldEarly = review.sold_too_early || [];
  setHtml('strategy-review-sold-early', soldEarly.length ?
    soldEarly.slice(0, 12).map(row => `<tr><td><b>${esc(row.stock_code)}</b><br><span class="muted">${esc(row.playbook || row.entry_type || '-')}</span></td><td>${esc(row.exit_class || '-')}<br><span class="muted">${esc(row.sell_at || '-')}</span></td><td class="${pnlClass(row.realized_pnl)}">${signedMoney(row.realized_pnl || 0)}<br><span class="muted">${pct(row.realized_rate)}</span></td><td class="warn">${pct(row.potential_left_rate)}<br><span class="muted">최고 ${pct(row.max_up_rate)}</span></td></tr>`).join('') :
    '<tr><td colspan="4" class="muted">너무 빨리 판 후보가 없습니다.</td></tr>');
  const reentries = review.missed_reentries || [];
  setHtml('strategy-review-missed-reentry', reentries.length ?
    reentries.slice(0, 12).map(row => `<tr><td><b>${esc(row.stock_name || row.stock_code)}</b><br><span class="muted">${esc(row.stock_code || '')}</span></td><td>${esc(row.playbook || row.entry_type || '-')}</td><td>${esc(row.reentry_engine_status || '-')}</td><td class="bad">${pct(row.max_up_rate)}</td><td>${esc((row.block_reasons || [])[0] || row.guard_status || row.router_status || '-')}</td></tr>`).join('') :
    '<tr><td colspan="5" class="muted">재진입 누락 후보가 없습니다.</td></tr>');
}
function renderSupplyShadow(supply) {
  if (!supply) return;
  const policy = supply.policy || {};
  const label = document.getElementById('supply-policy');
  label.textContent = policy.label || 'Shadow';
  label.className = 'badge warn';
  document.getElementById('supply-shadow-summary').textContent =
    `${policy.storage || 'supply_scores 저장'} · ${policy.live_order_effect || '실전 주문 영향 없음'} · ${policy.paper_order_effect || 'SQLite 모의 신규 진입 차단 가능'}`;
  const rows = supply.rows || [];
  document.getElementById('supply-shadow-scores').innerHTML = rows.length ?
    rows.map(row => {
      const program = row.program_status || 'not_collected';
      const programState = row.program_policy_state || program;
      const relaxed = row.program_legacy_block_relaxed ? ' · 기존차단 완화' : '';
      const blocked = row.blocked ? `<span class="bad">${esc(row.blocked_reason || '차단 후보')}</span>` : '<span class="muted">-</span>';
      return `<tr><td>${esc(row.evaluated_at)}</td><td><b>${esc(row.stock_name || row.stock_code)}</b><br><span class="muted">${esc(row.stock_code)}</span></td><td>${Number(row.total_score).toFixed(1)}</td><td>${Number(row.broker_score).toFixed(1)}</td><td>${Number(row.foreign_broker_score).toFixed(1)}</td><td>${Number(row.investor_score).toFixed(1)}</td><td>${Number(row.program_score).toFixed(1)}<br><span class="muted">${esc(programState + relaxed)}</span></td><td>${blocked}</td></tr>`;
    }).join('') :
    '<tr><td colspan="8" class="muted">저장된 수급 Shadow 점수가 없습니다.</td></tr>';
}
function renderSupplyPipeline(pipeline) {
  const target = document.getElementById('supply-pipeline-summary');
  if (!target || !pipeline) return;
  const counts = pipeline.counts || {};
  const event = pipeline.latest_leader_supply_event || {};
  const validation = pipeline.program_validation || {};
  const outcomes = pipeline.program_outcome_validation || {};
  target.innerHTML =
    `원천 broker ${Number(counts.broker_flow_snapshots || 0).toLocaleString()}건 · ` +
    `foreign ${Number(counts.foreign_broker_snapshots || 0).toLocaleString()}건 · ` +
    `investor ${Number(counts.investor_flow_snapshots || 0).toLocaleString()}건 · ` +
    `program ${Number(counts.program_trade_snapshots || 0).toLocaleString()}건 · ` +
    `supply_scores ${Number(counts.supply_scores || 0).toLocaleString()}건` +
    `<br>프로그램 정책: 차단 ${Number(validation.hard_block_count || 0)} · ` +
    `경고 ${Number(validation.watch_count || 0)} · 기존차단후보 ${Number(validation.legacy_block_candidate_count || 0)} · ` +
    `완화 ${Number(validation.legacy_block_relaxed_count || 0)}` +
    `<br>완화 후 성과: 평가 ${Number(outcomes.relaxed_evaluated_count || 0)} · ` +
    `3% 상승 ${Number(outcomes.relaxed_rallied_count || 0)} · 2% 하락 ${Number(outcomes.relaxed_dropped_count || 0)} · ` +
    `강한차단 평가 ${Number(outcomes.hard_block_evaluated_count || 0)}` +
    `<br>leader_supply: ${esc(event.event_type || '-')} · ${esc(event.occurred_at || '-')} · ${esc(event.message || '-')}`;
}
function renderIntradayMode(mode) {
  if (!mode) return;
  const badge = document.getElementById('intraday-mode-badge');
  badge.textContent = mode.label || mode.name || '확인 중';
  badge.className = `badge ${mode.entry_allowed ? 'ok' : 'warn'}`;
  document.getElementById('intraday-mode-summary').innerHTML = `
    <div class="flow-metric"><span class="label">신규 진입</span><b class="${mode.entry_allowed ? 'ok' : 'bad'}">${mode.entry_allowed ? '허용' : '차단'}</b><span class="muted">${esc(mode.name || '-')}</span></div>
    <div class="flow-metric"><span class="label">대장주</span><b class="${mode.leader_pass ? 'ok' : 'bad'}">${mode.leader_pass ? '통과' : '차단'}</b><span class="muted">${mode.leader_required ? '필수' : '중립'}</span></div>
    <div class="flow-metric"><span class="label">수급</span><b class="${mode.supply_pass ? 'ok' : 'bad'}">${mode.supply_pass ? '통과' : '차단'}</b><span class="muted">주문 전 재검증</span></div>
    <div class="flow-metric"><span class="label">시장 모드</span><b class="${mode.market_mode_pass ? 'ok' : 'bad'}">${mode.market_mode_pass ? '통과' : '차단'}</b><span class="muted">${esc(mode.details?.market_mode || '-')}</span></div>
  `;
  document.getElementById('intraday-mode-reason').textContent = mode.reason || '-';
}
function renderOrderSafety(safety) {
  if (!safety) return;
  const summary = safety.summary || {};
  const badge = document.getElementById('order-safety-badge');
  badge.textContent = summary.recent_failures ? `최근 실패 ${summary.recent_failures}건` : '최근 실패 없음';
  badge.className = `badge ${summary.recent_failures ? 'warn' : 'ok'}`;
  document.getElementById('order-safety-summary').innerHTML = `
    <div class="flow-metric"><span class="label">저장 이벤트</span><b>${Number(summary.total_events || 0).toLocaleString()}</b><span class="muted">주문 직전 검증</span></div>
    <div class="flow-metric"><span class="label">dry-run/모의 차단</span><b>${Number(summary.recent_failures || 0).toLocaleString()}</b><span class="muted">최근 ${Number(summary.window || 20)}건</span></div>
    <div class="flow-metric"><span class="label">마지막 사유</span><b>${esc(summary.latest_event_type || '-')}</b><span class="muted">${esc(summary.latest_at || '-')}</span></div>
  `;
  document.getElementById('order-safety-events').innerHTML = (safety.events || []).length ?
    safety.events.map(row => `<tr><td>${esc(row.occurred_at)}</td><td>${esc(row.stock_code)}</td><td>${esc(row.execution_mode)}</td><td><span class="chip bad">${esc(row.event_type)}</span><br><span class="muted">${esc(row.reason)}</span></td></tr>`).join('') :
    '<tr><td colspan="4" class="muted">저장된 주문 안전장치 차단 이벤트가 없습니다.</td></tr>';
}
function renderApiBudget(budget) {
  if (!budget) return;
  const summary = budget.summary || {};
  const badge = document.getElementById('api-budget-badge');
  badge.textContent = summary.backoff_groups ? `backoff ${summary.backoff_groups}개` : '정상';
  badge.className = `badge ${summary.backoff_groups ? 'warn' : 'ok'}`;
  const mode = summary.current_priority_mode || {};
  const quality = summary.data_quality || {};
  const warnings = quality.warnings || [];
  document.getElementById('api-budget-summary').innerHTML = `
    <div class="flow-metric"><span class="label">현재 우선순위</span><b>${esc(mode.label || '-')}</b><span class="muted">${Number(summary.window_count || 0).toLocaleString()}개 그룹</span></div>
    <div class="flow-metric"><span class="label">예산 부족 스킵</span><b>${Number(summary.recent_budget_skipped || 0).toLocaleString()}</b><span class="muted">최근 ${Number(summary.window || 20)}건</span></div>
    <div class="flow-metric"><span class="label">backoff 차단</span><b>${Number(summary.recent_backoff_blocked || 0).toLocaleString()}</b><span class="muted">최근 ${Number(summary.window || 20)}건</span></div>
    <div class="flow-metric"><span class="label">최근 429</span><b>${Number(summary.recent_429 || 0).toLocaleString()}</b><span class="muted">backoff 원인</span></div>
  `;
  document.getElementById('api-budget-mode').innerHTML = `${esc(mode.description || '-')}${warnings.length ? `<br>${warnings.map(w => `<span class="chip bad">${esc(w.label || w.code || w)}</span>`).join(' ')}` : ''}`;
  document.getElementById('api-budget-windows').innerHTML = (budget.windows || []).length ?
    budget.windows.map(row => `<tr><td><b>${esc(row.api_group)}</b><br><span class="muted">${esc(row.api_id || '-')}</span></td><td>${Number(row.used_count || 0).toLocaleString()} / ${Number(row.limit_count || 0).toLocaleString()}</td><td>${Number(row.remaining_count || 0).toLocaleString()}</td><td>${esc(row.backoff_until || '-')}</td></tr>`).join('') :
    '<tr><td colspan="4" class="muted">저장된 API 예산 창이 없습니다.</td></tr>';
}
function renderOperationWarning(warning) {
  if (!warning) return;
  const badge = document.getElementById('operation-warning-badge');
  const level = warning.level || 'ok';
  badge.textContent = warning.label || '정상';
  badge.className = `badge ${level === 'ok' ? 'ok' : (level === 'blocked' || level === 'bad' ? 'bad' : 'warn')}`;
  const buy = warning.today_new_buys || {};
  const stops = warning.stop_losses || {};
  const pnl = warning.daily_pnl || {};
  const quality = warning.data_quality || {};
  const api = warning.api_budget || {};
  const mode = warning.strategy_mode || {};
  const buyAllowedClass = warning.new_buy?.allowed ? 'ok' : 'bad';
  document.getElementById('operation-warning-summary').innerHTML = `
    <div class="flow-metric"><span class="label">오늘 신규 BUY</span><b class="${buy.blocked ? 'bad' : (buy.warning ? 'warn' : 'ok')}">${Number(buy.count || 0).toLocaleString()} / ${esc(buy.limit_label || '-')}</b><span class="muted">${esc(buy.scope || '오늘')}</span></div>
    <div class="flow-metric"><span class="label">손절</span><b class="${stops.blocked ? 'bad' : (stops.warning ? 'warn' : 'ok')}">${Number(stops.count || 0).toLocaleString()} / ${Number(stops.limit || 0).toLocaleString()}</b><span class="muted">일중 손절 제한</span></div>
    <div class="flow-metric"><span class="label">실현손익</span><b class="${Number(pnl.realized || 0) < 0 ? 'bad' : 'ok'}">${signedMoney(pnl.realized || 0)}</b><span class="muted">컷 ${signedMoney(pnl.loss_limit || 0)}</span></div>
    <div class="flow-metric"><span class="label">수급/대장주</span><b class="${quality.warning_count ? 'warn' : 'ok'}">${quality.warning_count ? '주의' : '최신'}</b><span class="muted">${esc(quality.summary || '-')}</span></div>
    <div class="flow-metric"><span class="label">429/backoff</span><b class="${api.backoff_groups || api.recent_429 ? 'warn' : 'ok'}">${Number(api.recent_429 || 0).toLocaleString()} / ${Number(api.backoff_groups || 0).toLocaleString()}</b><span class="muted">최근429 / backoff API</span></div>
    <div class="flow-metric"><span class="label">전략 모드</span><b class="${mode.entry_allowed ? 'ok' : 'bad'}">${esc(mode.label || '-')}</b><span class="muted">${mode.entry_allowed ? '진입 허용' : '진입 차단'}</span></div>
    <div class="flow-metric"><span class="label">신규 BUY</span><b class="${buyAllowedClass}">${warning.new_buy?.label || '-'}</b><span class="muted">${esc(warning.new_buy?.effect || '-')}</span></div>
    <div class="flow-metric"><span class="label">dry-run 차단</span><b class="${warning.dry_run?.count ? 'warn' : 'ok'}">${Number(warning.dry_run?.count || 0).toLocaleString()}</b><span class="muted">최근 차단 사유</span></div>
  `;
  const reasons = warning.reasons || [];
  const dryReasons = warning.dry_run?.reasons || [];
  const chips = [
    ...reasons.map(reason => `<span class="chip ${reason.severity === 'error' ? 'bad' : 'warn'}">${esc(reason.label || reason.code)}</span>`),
    ...dryReasons.map(reason => `<span class="chip bad">${esc(reason.reason || reason.event_type || '-')} ${Number(reason.count || 0).toLocaleString()}</span>`)
  ];
  document.getElementById('operation-warning-reasons').innerHTML = chips.length ?
    chips.join(' ') :
    '<span class="chip ok">무인 모의 시작 전 즉시 위험 신호 없음</span>';
}
function renderLeaderLearning(report) {
  if (!report) return;
  const summary = report.summary || {};
  document.getElementById('leader-learning-summary').innerHTML = `
    <div class="flow-metric"><span class="label">학습 후보</span><b>${Number(summary.candidate_count || 0).toLocaleString()}</b><span class="muted">TOP 후보 누적</span></div>
    <div class="flow-metric"><span class="label">사후 대장주</span><b>${Number(summary.actual_leader_count || 0).toLocaleString()}</b><span class="muted">적중률 ${pct(summary.hit_rate)}</span></div>
    <div class="flow-metric"><span class="label">평균 최고상승</span><b>${pct(summary.average_same_day_max_up_rate)}</b><span class="muted">당일 기준</span></div>
    <div class="flow-metric"><span class="label">blocked 복기</span><b>${Number(summary.blocked_helpful_count || 0).toLocaleString()} / ${Number(summary.blocked_missed_rally_count || 0).toLocaleString()}</b><span class="muted">적중 / 놓친 급등</span></div>
  `;
  document.getElementById('leader-learning-themes').innerHTML = (report.by_theme || []).length ?
    report.by_theme.map(row => `<tr><td><b>${esc(row.theme_name)}</b></td><td>${Number(row.candidate_count || 0).toLocaleString()}</td><td>${Number(row.actual_leader_count || 0).toLocaleString()}</td><td>${pct(row.hit_rate)}</td><td>${pct(row.average_same_day_max_up_rate)}</td></tr>`).join('') :
    '<tr><td colspan="5" class="muted">저장된 테마별 학습 결과가 없습니다.</td></tr>';
  document.getElementById('leader-learning-components').innerHTML = (report.component_performance || []).length ?
    report.component_performance.map(row => `<tr><td><b>${esc(row.label || row.component)}</b></td><td>${Number(row.candidate_count || 0).toLocaleString()}</td><td>${pct(row.hit_rate)}</td><td>${pct(row.average_performance_score)}</td></tr>`).join('') :
    '<tr><td colspan="4" class="muted">저장된 구성요소별 성과가 없습니다.</td></tr>';
  const reasons = report.blocked_review?.by_reason || [];
  document.getElementById('leader-learning-blocked').innerHTML = reasons.length ?
    reasons.map(row => `<tr><td>${esc(row.reason)}</td><td>${Number(row.candidate_count || 0).toLocaleString()}</td><td class="ok">${Number(row.helpful_count || 0).toLocaleString()}</td><td class="bad">${Number(row.missed_rally_count || 0).toLocaleString()}</td><td>${pct(row.average_performance_score)}</td></tr>`).join('') :
    '<tr><td colspan="5" class="muted">저장된 blocked 복기 결과가 없습니다.</td></tr>';
}
function signalState(signal) {
  if (signal.action === 'executed') return {text:'주문 실행', cls:'ok'};
  if (signal.signal_pass) return {text:'조건 통과', cls:'ok'};
  if (signal.action === 'failed') return {text:'처리 실패', cls:'bad'};
  return {text:'관찰', cls:'warn'};
}
function signalReason(signal) {
  if (signal.message) return signal.message;
  if (signal.signal_pass) return '매수 조건을 통과했습니다.';
  const failed = [];
  if (!signal.volume_pass) failed.push('거래량');
  if (!signal.breakout_pass) failed.push('돌파');
  if (!signal.moving_average_pass) failed.push('이동평균');
  if (!signal.chase_pass) failed.push('추격제한');
  return failed.length ? `${failed.join(', ')} 조건에서 걸러짐` : '조건 미충족';
}
function renderAutomationFlow(flow) {
  if (!flow) return;
  const mode = document.getElementById('automation-mode');
  mode.textContent = flow.mode_label || '확인 중';
  mode.className = `badge ${flow.running ? 'ok' : 'warn'}`;
  document.getElementById('automation-summary').innerHTML = `
    <div class="flow-metric"><span class="label">오늘 평가</span><b>${flow.signal_counts.total}</b><span class="muted">통과 ${flow.signal_counts.passed} · 실행 ${flow.signal_counts.executed}</span></div>
    <div class="flow-metric"><span class="label">오늘 주문</span><b>${flow.order_counts.total}</b><span class="muted">체결 ${flow.order_counts.filled} · 거절 ${flow.order_counts.rejected}</span></div>
    <div class="flow-metric"><span class="label">최근 수집</span><b>${esc(flow.latest_run?.status || '-')}</b><span class="muted">${esc(flow.latest_run?.completed_at || flow.latest_run?.started_at || '-')}</span></div>
    <div class="flow-metric"><span class="label">최근 평가 시각</span><b>${esc(flow.signal_counts.latest_signal_time || '-')}</b><span class="muted">5초마다 화면 갱신</span></div>
  `;
  document.getElementById('automation-signals').innerHTML = flow.recent_signals.length ?
    flow.recent_signals.map(signal => {
      const state = signalState(signal);
      return `<div class="flow-item"><span class="chip ${state.cls}">${state.text}</span> <b>${esc(signal.stock_name || signal.stock_code)}</b><br><span class="muted">${esc(signal.signal_time)} · 현재가 ${money(signal.current_price)} · 거래량비 ${signal.volume_ratio == null ? '-' : Number(signal.volume_ratio).toFixed(2)} · 등락 ${pct(signal.day_gain_rate)}</span><br><span>${esc(signalReason(signal))}</span></div>`;
    }).join('') :
    '<div class="flow-item muted">오늘 저장된 전략 평가가 아직 없습니다.</div>';
  document.getElementById('automation-orders').innerHTML = flow.recent_orders.length ?
    flow.recent_orders.map(order => `<div class="flow-item"><span class="chip ${order.status === 'filled' ? 'ok' : 'bad'}">${esc(order.status)}</span> <b>${esc(order.stock_name || order.stock_code)}</b><br><span class="muted">${esc(order.side)} ${Number(order.quantity).toLocaleString()}주 · ${esc(order.completed_at || order.requested_at)} · ${money(order.fill_price)}</span><br><span>${esc(order.reason || order.rejection_reason || '-')}</span></div>`).join('') :
    '<div class="flow-item muted">최근 모의 주문은 없습니다.</div>';
}
function renderLeaderThemes(leader) {
  if (!leader) return;
  document.getElementById('leader-summary').textContent = leader.latest_trade_date ?
    `기준 ${leader.latest_trade_date} · 후보 ${leader.scores.length}개 · 수집 순위 ${leader.rank_items.length}개` :
    '아직 대장주 점수 계산 이력이 없습니다.';
  document.getElementById('leader-scores').innerHTML = leader.scores.length ?
    leader.scores.map(row => {
      const roleClass = row.blocked ? 'bad' : (row.role === 'leader' ? 'ok' : (row.role === 'second' ? 'warn' : ''));
      const reasons = (row.reasons || []).join(' · ');
      const blocked = row.blocked_reason ? `${row.blocked_reason}<br>` : '';
      return `<tr><td class="${roleClass}">${esc(row.role)}</td><td><b>${esc(row.stock_name || row.stock_code)}</b><br><span class="muted">${esc(row.stock_code)} · ${esc(row.theme_name || '-')}</span></td><td><b>${Number(row.total_score).toFixed(1)}</b></td><td>${Number(row.trade_amount_score).toFixed(1)}</td><td>${Number(row.rank_score).toFixed(1)}</td><td>${Number(row.theme_score).toFixed(1)}</td><td>${Number(row.persistence_score).toFixed(1)}</td><td>${Number(row.chart_score).toFixed(1)}</td><td>${Number(row.risk_score).toFixed(1)}</td><td title="${esc(reasons)}">${blocked}${esc(reasons || '-')}</td></tr>`;
    }).join('') :
    '<tr><td colspan="10" class="muted">저장된 대장주 후보 점수가 없습니다.</td></tr>';
  document.getElementById('theme-journals').innerHTML = leader.theme_journals.length ?
    leader.theme_journals.map(row => `<tr><td>${esc(row.theme_date)}</td><td><b>${esc(row.theme_name)}</b></td><td>${esc(row.strength)}</td><td>${Number(row.member_count || 0).toLocaleString()}</td><td>${esc(row.notes || '-')}</td></tr>`).join('') :
    '<tr><td colspan="5" class="muted">최근 30일 테마 기록이 없습니다.</td></tr>';
  document.getElementById('market-rank-status').innerHTML = leader.rank_items.length ?
    leader.rank_items.map(row => {
      const trade = row.trade_amount == null ? '-' : `${Math.round(Number(row.trade_amount) / 100000000).toLocaleString()}억원`;
      return `<tr><td>${esc(row.rank_type)}</td><td>${esc(row.observed_at || row.collected_at || '-')}</td><td>${row.rank || '-'}위 · ${esc(row.stock_name || row.stock_code)}</td><td>${trade}</td></tr>`;
    }).join('') :
    '<tr><td colspan="4" class="muted">저장된 시장 순위 공통 스냅샷이 없습니다.</td></tr>';
}
function renderThemeTrends(trends) {
  if (!trends) return;
  const rows = trends.items || [];
  const top = rows[0];
  document.getElementById('theme-trend-count').textContent = `${rows.length.toLocaleString()}개`;
  document.getElementById('theme-trend-top').textContent = top ? top.theme_name : '-';
  document.getElementById('theme-trend-top-detail').textContent = top ?
    `신뢰 ${Number(top.trust_score).toFixed(1)} · 등장 ${top.appearance_days}일 · 최근 ${top.latest_date || '-'}` :
    '최근 30일 저장된 테마 기록이 없습니다.';
  const strongCount = rows.reduce((sum, row) => sum + Number(row.strong_days || 0), 0);
  document.getElementById('theme-trend-strong').textContent = `${strongCount.toLocaleString()}회`;
  const levelLabel = value => value === 'high' ? '높음' : (value === 'medium' ? '중간' : '낮음');
  const levelClass = value => value === 'high' ? 'ok' : (value === 'medium' ? 'warn' : '');
  document.getElementById('theme-trends').innerHTML = rows.length ?
    rows.map(row => `<tr><td class="${levelClass(row.trust_level)}">${levelLabel(row.trust_level)}</td><td><b>${esc(row.theme_name)}</b></td><td><b>${Number(row.trust_score).toFixed(1)}</b></td><td>${Number(row.appearance_days || 0).toLocaleString()}일</td><td>${Number(row.journal_days || 0).toLocaleString()} / ${Number(row.score_days || 0).toLocaleString()}</td><td>강 ${Number(row.strong_days || 0)} · 중 ${Number(row.medium_days || 0)} · 약 ${Number(row.weak_days || 0)}</td><td>${Number(row.leader_count || 0).toLocaleString()} / ${Number(row.second_count || 0).toLocaleString()}</td><td>${Number(row.average_leader_score || 0).toFixed(1)}</td><td>${esc(row.latest_date || '-')}</td></tr>`).join('') :
    '<tr><td colspan="9" class="muted">최근 30일 저장된 테마 저널 또는 대장주 테마 점수가 없습니다.</td></tr>';
}
async function refresh() {
  try {
    const d = await fetch('/api/status', {cache:'no-store'}).then(r => r.json());
    const rt = document.getElementById('runtime');
    rt.textContent = d.runtime.running ? `실행 중 (PID ${d.runtime.pid})` :
      (d.runtime.stop_requested ? '종료 대기 중' : '정지');
    rt.className = cls(d.runtime.running, d.runtime.stop_requested);
    const api = document.getElementById('api');
    api.textContent = d.api.credentials_configured ?
      `${d.api.server_mode} · ${d.api.connection_label}` : '인증키 미설정';
    api.className = cls(d.api.credentials_configured && d.api.recent_success, d.api.credentials_configured);
    const market = document.getElementById('market');
    market.textContent = d.market.open ? '정규장 운영 중' : `장 닫힘 · ${d.market.day_name}`;
    market.className = cls(d.market.open, !d.market.open);
    const gate = document.getElementById('market-mode-gate');
    gate.textContent = d.market_mode_gate.enabled ?
      `${d.market_mode_gate.manual_mode} · ${d.market_mode_gate.entry_allowed ? '진입 허용' : '진입 차단'} · ${d.market_mode_gate.source}` :
      '비활성';
    if (d.market_mode_gate.enabled && d.market_mode_gate.requested_mode && d.market_mode_gate.requested_mode !== d.market_mode_gate.manual_mode) {
      gate.textContent += ` · 요청 ${d.market_mode_gate.requested_mode}`;
    }
    if (d.market_mode_gate.shadow_mode) {
      gate.textContent += ` · shadow ${d.market_mode_gate.shadow_mode}`;
    }
    if (d.market_mode_gate.order_effect) {
      gate.textContent += ` · ${d.market_mode_gate.order_effect}`;
    }
    if (d.market_mode_auto) {
      const auto = d.market_mode_auto;
      const ma = auto.above_ma20 === 1 || auto.above_ma20 === true ? '20일선 위' : '20일선 아래';
      const amount = auto.trade_amount ? money(auto.trade_amount) : '거래대금 없음';
      gate.textContent += ` · KOSDAQ ${auto.mode}/${ma}/${amount}`;
    }
    gate.className = cls(d.market_mode_gate.entry_allowed, d.market_mode_gate.enabled);
    const col = document.getElementById('collection');
    col.textContent = d.collection.latest_at || '수집 이력 없음';
    col.className = cls(d.collection.fresh, !!d.collection.latest_at);
    document.getElementById('paper-count').textContent = `${d.accounts.length}개`;
    document.getElementById('accounts').innerHTML = d.accounts.length ?
      d.accounts.map(a => `<div class="account-row"><b>${esc(a.name)}</b><div class="metric">${money(a.total_equity)}</div><span class="muted">현금 ${money(a.cash_balance)} · 평가 ${money(a.market_value)} · 보유 ${a.position_count}종목</span></div>`).join('') :
      '<p>생성된 모의계좌가 없습니다.</p>';
    document.getElementById('alerts').innerHTML = d.alerts.length ?
      d.alerts.map(a => `<p class="bad"><b>${esc(a.severity)}</b> · ${esc(a.message)}</p>`).join('') :
      '<p class="ok">활성 경보 없음</p>';
    const universe = d.universe;
    document.getElementById('universe-running').textContent =
      universe.runtime.running || universe.runtime.starting ? '수집 중' : '정지';
    document.getElementById('universe-running').className =
      `badge ${universe.runtime.running || universe.runtime.starting ? 'ok' : ''}`;
    document.getElementById('universe-summary').textContent =
      `등록 ${universe.registered_count}종목 · 완료 ${universe.completed_count} · 대기 ${universe.pending_count} · 실패 ${universe.failed_count}` +
      (universe.updated_at ? ` · 최근 ${universe.updated_at}` : '');
    renderAutomationFlow(d.automation_flow);
    renderOperationWarning(d.strategy.operation_warning);
    renderIntradayMode(d.strategy.intraday_mode);
    renderOrderSafety(d.strategy.order_safety);
    renderApiBudget(d.strategy.api_budget);
    renderPerformance(d.paper_performance);
    const strategy = d.strategy;
    renderSupplyShadow(strategy.supply_shadow);
    renderSupplyPipeline(strategy.supply_pipeline);
    if (!replayFiltersApplied && strategy.replay_review) {
      renderReplayReview(strategy.replay_review);
    } else if (!replayFiltersApplied && !latestReplayReview) {
      applyReplayFilters();
    }
    renderTradingReview(strategy.trading_review);
    renderThemeTrends(d.theme_trends);
    renderLeaderThemes(d.leader_themes);
    renderLeaderLearning(d.leader_themes.learning_report);
    const candidates = d.candidates;
    const categoryText = (candidates.by_stock_category || [])
      .filter(row => Number(row.candidate_count || 0) > 0)
      .map(row => `${row.label || row.category} ${Number(row.candidate_count || 0).toLocaleString()}개`)
      .join(' · ');
    document.getElementById('candidate-summary').textContent = candidates.run ?
      `기준 ${candidates.run.as_of} · 전체 ${candidates.run.universe_count} · 적격 ${candidates.run.eligible_count} · 선정 ${candidates.run.selected_count}` :
      '아직 후보 계산 이력이 없습니다.';
    if (candidates.run && categoryText) {
      document.getElementById('candidate-summary').textContent += ` · ${categoryText}`;
    }
    document.getElementById('candidate-scores').innerHTML = candidates.rows.length ?
      candidates.rows.map(c => {
        const change = c.score_change == null ? '-' : `${Number(c.score_change) >= 0 ? '+' : ''}${Number(c.score_change).toFixed(2)}`;
        const reason = c.excluded_reason || (c.selected ? '상위 후보 선정' : '적격, 순위 밖');
        const cup = c.features && c.features.cup_handle_state
          ? `컵앤핸들:${c.features.cup_handle_state}(${Number(c.features.cup_handle_score).toFixed(1)})`
          : '';
        const triangle = c.features && c.features.ascending_triangle_state
          ? `상승삼각형:${c.features.ascending_triangle_state}(${Number(c.features.ascending_triangle_score).toFixed(1)})`
          : '';
        const flag = c.features && c.features.bull_flag_state
          ? `상승플래그:${c.features.bull_flag_state}(${Number(c.features.bull_flag_score).toFixed(1)})`
          : '';
        const pennant = c.features && c.features.bull_pennant_state
          ? `상승페넌트:${c.features.bull_pennant_state}(${Number(c.features.bull_pennant_score).toFixed(1)})`
          : '';
        const box = c.features && c.features.box_breakout_state
          ? `박스:${c.features.box_breakout_state}(${Number(c.features.box_breakout_score).toFixed(1)})`
          : '';
        const bottom = c.features && c.features.double_bottom_state
          ? `이중바닥:${c.features.double_bottom_state}(${Number(c.features.double_bottom_score).toFixed(1)})`
          : '';
        const top = c.features && c.features.double_top_state
          ? `이중천장:${c.features.double_top_state}(${Number(c.features.double_top_score).toFixed(1)})`
          : '';
        const inverseHs = c.features && c.features.inverse_head_shoulders_state
          ? `역H&S:${c.features.inverse_head_shoulders_state}(${Number(c.features.inverse_head_shoulders_score).toFixed(1)})`
          : '';
        const hs = c.features && c.features.head_shoulders_state
          ? `H&S:${c.features.head_shoulders_state}(${Number(c.features.head_shoulders_score).toFixed(1)})`
          : '';
        const patternText = [...(c.patterns || []), cup, triangle, flag, pennant, box, bottom, top, inverseHs, hs].filter(Boolean).join(', ') || '-';
        const intraday = c.intraday;
        const vwapText = intraday
          ? `${Number(intraday.vwap).toLocaleString(undefined, {maximumFractionDigits:1})} (${Number(intraday.vwap_distance_rate) >= 0 ? '+' : ''}${(Number(intraday.vwap_distance_rate) * 100).toFixed(2)}%)<br><span class="muted">리클레임 ${esc(intraday.reclaim_state)} · 바운스 ${esc(intraday.bounce_state)}<br>스윕 ${esc(intraday.liquidity_sweep_side)}:${esc(intraday.liquidity_sweep_state)} (${Number(intraday.liquidity_sweep_score).toFixed(1)})<br>FVG ${esc(intraday.fvg_side)}:${esc(intraday.fvg_state)} (${Number(intraday.fvg_score).toFixed(1)}, 충전 ${intraday.fvg_fill_rate == null ? '-' : `${(Number(intraday.fvg_fill_rate) * 100).toFixed(0)}%`})<br>OB ${esc(intraday.order_block_side)}:${esc(intraday.order_block_state)} (${Number(intraday.order_block_score).toFixed(1)})<br>${esc(intraday.source_data_time)}</span>`
          : '-';
        const infoClass = c.information_blocked ? 'bad' : (Number(c.information_score) > 0 ? 'ok' : '');
        const compositeClass = c.composite_status === 'buy_candidate' ? 'ok' : (c.composite_status === 'blocked' ? 'bad' : 'warn');
        const componentText = (c.composite_components || []).map(x => `${x.name} ${Number(x.weighted_score).toFixed(1)}`).join(' · ');
        const statusLabels = {buy_candidate:'매수 후보', observe:'관찰', excluded:'제외', blocked:'차단', insufficient_data:'데이터 부족'};
        const dailyTags = [];
        if (c.features && c.features.recent5_high_breakout) dailyTags.push('5일고점 돌파');
        else if (c.features && c.features.recent5_high_near) dailyTags.push('5일고점 근접');
        if (c.features && c.features.new_60d_high) dailyTags.push('60일 신고가');
        if (c.features && c.features.resistance_breakout) dailyTags.push('저항 돌파');
        else if (c.features && c.features.near_resistance) dailyTags.push('저항 근처');
        if (c.features && c.features.healthy_gap) dailyTags.push('건강한 갭');
        if (c.features && (c.features.gap_overheated || c.features.day_gain_overheated)) dailyTags.push('과열 주의');
        if (c.features && c.features.leader_theme_candidate) dailyTags.push('대장주 후보');
        if (c.features && c.features.market_rank && c.features.market_rank.trade_amount) {
          dailyTags.push(`0187 거래대금 ${c.features.market_rank.trade_amount.rank}위`);
        }
        const dailyText = dailyTags.length ? `<br><span class="muted">${esc(dailyTags.join(' · '))}</span>` : '';
        return `<tr><td>${c.rank || '-'}</td><td><b>${esc(c.stock_name || c.stock_code)}</b><br><span class="muted">${esc(c.stock_code)}</span></td><td>${Number(c.total_score).toFixed(2)} / ${change}</td><td class="${compositeClass}" title="${esc(componentText)}"><b>${Number(c.composite_score || 0).toFixed(1)}점</b><br>${esc(statusLabels[c.composite_status] || c.composite_status)} · 신뢰도 ${(Number(c.composite_confidence || 0) * 100).toFixed(0)}%</td><td>${Number(c.technical_score).toFixed(2)}${dailyText}</td><td>${Number(c.pattern_score).toFixed(2)}</td><td class="${infoClass}">${Number(c.information_score || 0) >= 0 ? '+' : ''}${Number(c.information_score || 0).toFixed(2)}${c.information_blocked ? '<br>진입 차단' : ''}</td><td>${c.relative_strength == null ? '-' : `${(Number(c.relative_strength) * 100).toFixed(2)}%`}</td><td>${c.rsi == null ? '-' : Number(c.rsi).toFixed(1)} / ${c.atr == null ? '-' : Number(c.atr).toFixed(1)}</td><td>${esc(patternText)}</td><td>${vwapText}</td><td>${c.watched ? '예' : '아니오'}</td><td title="${esc((c.reasons || []).join(' · '))}">${esc(reason)}</td></tr>`;
      }).join('') :
      '<tr><td colspan="13" class="muted">저장된 후보 점수가 없습니다.</td></tr>';
    const rankings = d.realtime_rankings;
    document.getElementById('realtime-rank-summary').textContent = rankings.latest_at ?
      `구분 ${rankings.query_type} · ${rankings.latest_at} · ${rankings.rows.length}개 · ${rankings.scoring_effect}` :
      '수집 이력이 없습니다.';
    document.getElementById('realtime-rankings').innerHTML = rankings.rows.length ?
      rankings.rows.map(r => {
        const change = r.rank_change == null ? '-' : `${r.rank_change > 0 ? '+' : ''}${r.rank_change}${r.rank_change_sign || ''}`;
        const rate = r.base_compare_change_rate == null ? '-' : `${Number(r.base_compare_change_rate) > 0 ? '+' : ''}${Number(r.base_compare_change_rate).toFixed(2)}%`;
        return `<tr><td>${r.big_data_rank || '-'}</td><td><b>${esc(r.stock_name || r.stock_code)}</b><br><span class="muted">${esc(r.stock_code)}</span></td><td>${esc(change)}</td><td>${money(r.past_current_price)}</td><td>${esc(rate)}</td><td>${esc(r.observed_at || '-')}</td></tr>`;
      }).join('') :
      '<tr><td colspan="6" class="muted">저장된 실시간 조회종목 순위가 없습니다.</td></tr>';
    document.getElementById('information-items').innerHTML = d.information.items.length ?
      d.information.items.map(i => `<tr><td>${esc(i.published_at)}</td><td>${esc(i.stock_name || i.stock_code || '-')}</td><td>${esc(i.source)}</td><td>${esc(i.event_type)}</td><td>${(Number(i.confidence) * 100).toFixed(0)}%</td><td class="${Number(i.current_score) > 0 ? 'ok' : (Number(i.current_score) < 0 ? 'bad' : '')}">${Number(i.current_score) >= 0 ? '+' : ''}${Number(i.current_score).toFixed(2)}</td><td class="${i.entry_blocked ? 'bad' : ''}">${i.entry_blocked ? '차단' : '-'}</td><td><a href="${esc(i.url || '#')}" target="_blank" rel="noopener">${esc(i.title)}</a></td></tr>`).join('') :
      '<tr><td colspan="8" class="muted">저장된 뉴스·공시가 없습니다.</td></tr>';
    const ai = d.ai_shadow;
    document.getElementById('ai-summary').textContent =
      `${ai.provider}:${ai.model} · 프롬프트 ${ai.prompt_version} · ${ai.enabled ? '자동 분석 켜짐' : '자동 분석 꺼짐'} · ${ai.configured ? 'API 키 설정됨' : 'API 키 미설정'} · 주문 반영 없음`;
    document.getElementById('ai-analyses').innerHTML = ai.rows.length ?
      ai.rows.map(a => {
        const cost = a.estimated_cost == null ? '-' : `$${Number(a.estimated_cost).toFixed(6)}`;
        const performance = a.forward_return == null ? '대기' : `${(Number(a.forward_return) * 100).toFixed(2)}% · ${esc(a.performance_status)}`;
        const rationale = a.status === 'success'
          ? `${esc(a.summary || '')}<br><span class="muted">위험: ${esc((a.risks || []).join(' · ') || '-')}<br>촉매: ${esc((a.catalysts || []).join(' · ') || '-')}</span>`
          : `<span class="bad">${esc(a.error_type || '오류')}: ${esc(a.error_message || '')}</span>`;
        return `<tr><td>${esc(a.created_at)}</td><td>${esc(a.stock_name || a.stock_code)}</td><td>${esc(a.provider)}:${esc(a.model)}<br><span class="muted">${esc(a.prompt_version)}</span></td><td>${esc(a.algorithm_action)}<br>${Number(a.algorithm_score).toFixed(1)}점</td><td>${esc(a.ai_action || '-')}<br>${a.ai_score == null ? '-' : `${Number(a.ai_score).toFixed(1)}점 · ${(Number(a.ai_confidence) * 100).toFixed(0)}%`}</td><td>${esc(a.agreement || '-')}</td><td>${rationale}</td><td>${a.input_tokens || '-'} / ${a.output_tokens || '-'}<br>${a.latency_ms == null ? '-' : `${a.latency_ms}ms`} · ${cost}</td><td>${performance}</td></tr>`;
      }).join('') :
      '<tr><td colspan="9" class="muted">저장된 AI 분석이 없습니다.</td></tr>';
    document.getElementById('strategy-active').textContent = strategy.runtime_enabled ? '자동 전략 켜짐' : '자동 전략 꺼짐';
    document.getElementById('strategy-active').className = `badge ${strategy.runtime_enabled ? 'ok' : 'warn'}`;
    const smallMode = document.getElementById('small-account-mode');
    if (smallMode) {
      smallMode.textContent = `소액 공격 모드: ${strategy.small_account_aggressive ? '켜짐' : '꺼짐'} · SQLite 모의계좌 전용`;
      smallMode.className = `muted ${strategy.small_account_aggressive ? 'ok' : ''}`;
    }
    const abMode = document.getElementById('strategy-ab-mode');
    if (abMode) {
      const mode = strategy.strategy_ab_active_mode || 'remake';
      abMode.textContent = `전략 A/B 실제모드: ${mode === 'normal' ? '일반' : '리메이크'} · 반대 모드는 shadow 저장`;
      abMode.className = `muted ${mode === 'remake' ? 'ok' : 'warn'}`;
    }
    document.getElementById('strategy-versions').innerHTML = strategy.versions.length ?
      strategy.versions.map(v => {
        const status = `${v.active ? '활성' : '비활성'} · 설정 #${v.config_id || '-'}`;
        const summary = v.summary || v.description || '설정 상세 없음';
        const title = `${v.strategy_name} v${v.version} - ${summary}`;
        return `<div class="strategy-row" title="${esc(title)}"><b>${esc(v.strategy_name)} v${esc(v.version)}</b><div class="muted">${esc(status)}</div><div class="strategy-summary">${esc(summary)}</div></div>`;
      }).join('') :
      '<p class="muted">등록된 전략 버전이 없습니다.</p>';
    const risk = strategy.risk_settings;
    const takeProfitText = risk.take_profit_enabled ?
      `+${(risk.take_profit_rate * 100).toFixed(1)}%` :
      '고정 익절 비활성';
    document.getElementById('risk-settings').innerHTML =
      `<div class="account-row"><b>손절 / 익절</b><div class="metric">${(risk.stop_loss_rate * 100).toFixed(1)}% / ${takeProfitText}</div></div>` +
      `<div class="account-row"><b>ATR / 본전 보호</b><div class="muted">ATR ${risk.atr_period}봉 × ${risk.atr_stop_multiple.toFixed(1)} · +${(risk.breakeven_activation_rate * 100).toFixed(1)}%부터 매수가 보호</div></div>` +
      `<div class="account-row"><b>동적 트레일링</b><div class="muted">+${(risk.trailing_activation_rate * 100).toFixed(1)}% 활성 · 강세 ${(risk.strong_trend_trailing_drawdown_rate * 100).toFixed(1)}% / 중립 ${(risk.trailing_drawdown_rate * 100).toFixed(1)}% / 약세 ${(risk.weak_trend_trailing_drawdown_rate * 100).toFixed(1)}%</div></div>` +
      `<div class="account-row"><b>익절 운영</b><div class="muted">${risk.take_profit_enabled ? `기본 +${(risk.take_profit_rate * 100).toFixed(1)}% · 강세 +${(risk.strong_trend_take_profit_rate * 100).toFixed(1)}%` : '고정 전량 익절 없음 · 매매 계획의 1차 익절/본절/트레일링 중심'}</div></div>` +
      `<div class="account-row"><b>포지션 제한</b><div class="muted">종목당 ${(risk.max_position_rate * 100).toFixed(0)}% · 최대 ${risk.max_positions}종목</div></div>`;
    document.getElementById('risk-positions').innerHTML = d.risk_positions.length ?
      d.risk_positions.map(p => {
        const rate = p.return_rate == null ? null : Number(p.return_rate) * 100;
        const protections = [p.breakeven_active ? '본전' : '', p.trailing_active ? '트레일링' : ''].filter(Boolean).join(' · ') || '초기 손절';
        const partialState = p.partial_exit_dashboard || {};
        const stateChips = [
          partialState.target_raised ? `<span class="chip ok" title="상향 목표 ${money(partialState.raised_target)}">목표상향</span>` : '',
          partialState.second_deferred ? `<span class="chip warn">2차지연</span>` : '',
          partialState.vwap_state && partialState.vwap_state !== 'unknown' ? `<span class="chip ${partialState.vwap_state === 'actual_breakdown' ? 'bad' : (partialState.vwap_state === 'shakeout_recovered' ? 'ok' : 'warn')}" title="${esc(partialState.vwap_reason || '')}">VWAP ${esc(partialState.vwap_label || partialState.vwap_state)}</span>` : '',
          partialState.reentry_locked ? `<span class="chip bad" title="${esc(partialState.reentry_lock_reason || '')}">재진입잠금</span>` : ''
        ].filter(Boolean).join(' ');
        const partialPlan = p.partial_exit_strategy ?
          `${esc(p.partial_exit_strategy)}<br><span class="muted">1차 ${p.first_exit_completed ? '완료' : '대기'} ${money(p.first_target_price)} · 잔여 ${Number(p.residual_quantity || 0).toLocaleString()}주 · ${esc(p.current_zone || '-')}</span>${stateChips ? `<div style="margin-top:4px">${stateChips}</div>` : ''}` :
          '<span class="muted">계획 없음</span>';
        return `<tr><td><b>${esc(p.account_name)}</b><br>${esc(p.stock_code)}</td><td>${money(p.average_price)} / ${money(p.current_price)}</td><td class="${rate != null && rate >= 0 ? 'ok' : 'bad'}">${rate == null ? '-' : `${rate.toFixed(2)}%`}</td><td>${money(p.highest_price)}</td><td>${money(p.initial_stop_price)} / ${money(p.current_stop_price)}</td><td>${money(p.current_take_profit_price)}</td><td title="${esc(p.residual_exit_rule || p.exit_condition || '')}">${partialPlan}</td><td>${esc(protections)}</td><td>${esc(p.trend_strength || '-')} (${p.trend_score ?? '-'})</td><td title="${esc(p.last_rule_reason)}">${esc(p.expected_exit_reason || '-')}</td><td>${esc(p.risk_evaluated_at || p.price_time || '-')}</td></tr>`;
      }).join('') :
      '<tr><td colspan="11" class="muted">보유 중이거나 위험관리를 평가한 모의 종목이 없습니다.</td></tr>';
    document.getElementById('strategy-decisions').innerHTML = strategy.recent_decisions.length ?
      strategy.recent_decisions.map(x => {
        const components = x.components || [];
        const important = components.filter(c =>
          ['chart_trade', 'supply_flow', 'pattern', 'information', 'volume', 'liquidity', 'breakout', 'opening_range', 'pullback', 'order_flow', 'vwap', 'moving_average'].includes(c.name)
        );
        const chips = important.map(c => {
          const score = Number(c.weighted_score || 0);
          const rawScore = Number(c.score || 0);
          const clsName = (c.name === 'information' || c.name === 'chart_trade' || c.name === 'supply_flow') && rawScore < 0 ? 'bad' : (score >= 5 || rawScore >= 80 ? 'ok' : (score > 0 || rawScore >= 50 ? 'warn' : 'bad'));
          const label = {chart_trade:'차트', supply_flow:'수급', pattern:'패턴', information:'뉴스', volume:'거래량', liquidity:'거래대금', breakout:'돌파', opening_range:'ORB', pullback:'눌림', order_flow:'호가', vwap:'VWAP', moving_average:'이평'}[c.name] || c.name;
          return `<span class="chip ${clsName}" title="${esc(c.reason)}">${esc(label)} ${score >= 0 ? '+' : ''}${score.toFixed(1)}</span>`;
        }).join(' ');
        const summary = chips ? `<div>${chips}</div>` : '';
        const plan = x.trade_plan;
        const partialText = plan && plan.partial_exit_executed ?
          ` · 반익절 ${Number(plan.partial_exit_quantity || 0).toLocaleString()}주 완료` :
          '';
        const setup = plan && plan.setup ? plan.setup : {};
        const structureText = plan ? [
          setup.breakout_hold_pass ? `돌파유지 ${money(setup.breakout_level)}` : '',
          setup.opening_range_pass ? `ORB ${money(setup.opening_range_low)}~${money(setup.opening_range_high)}` : '',
          setup.pullback_support_pass ? `눌림지지 ${money(setup.pullback_support_level)}` : '',
          setup.intraday_midline ? `중심선 ${money(setup.intraday_midline)}` : '',
          setup.deep_pullback_blocked ? '깊은눌림 차단' : '',
          setup.order_flow_score ? `호가 ${Number(setup.order_flow_score).toFixed(0)}점` : '',
          setup.ask_wall_absorption ? '매도벽 흡수' : '',
          setup.bid_wall_risk ? '매수벽 위험' : ''
        ].filter(Boolean).join(' · ') : '';
        const chartText = plan ? [
          setup.chart_daily_setup ? `일봉 ${setup.chart_daily_setup}` : '',
          setup.chart_intraday_trigger ? `분봉 ${setup.chart_intraday_trigger}` : '',
          setup.chart_risk_blocked ? `차단 ${setup.chart_block_reason || '차트 위험'}` : '',
          setup.second_take_profit_price ? `2차익절 ${money(setup.second_take_profit_price)}` : ''
        ].filter(Boolean).join(' · ') : '';
        const replay = x.replay || {};
        const replaySupply = replay.supply_score || setup.supply_flow || {};
        const replayLeader = replay.leader_score || {};
        const replayText = [
          replaySupply.total_score != null ? `수급 ${Number(replaySupply.total_score).toFixed(1)}점` : '',
          replaySupply.blocked ? `수급차단 ${replaySupply.blocked_reason || '-'}` : '',
          replayLeader.role ? `대장주 ${replayLeader.role} ${Number(replayLeader.total_score || 0).toFixed(1)}점` : '',
          replay.candles ? `캔들 ${replay.candles.length}개 저장` : ''
        ].filter(Boolean).join(' · ');
        const dataPolicy = x.data_availability_policy || setup.data_availability_policy || (replay.details && replay.details.data_availability_policy) || {};
        const dataChips = [
          Number(x.occurrence_count || 1) > 1 ? `<span class="chip warn" title="마지막 관측 ${esc(x.last_seen_at || '-')}">반복 ${Number(x.occurrence_count).toLocaleString()}회</span>` : '',
          dataPolicy.supply_status === 'missing' ? '<span class="chip bad">수급 없음</span>' : '',
          dataPolicy.leader_status === 'missing' ? '<span class="chip bad">대장주 점수 없음</span>' : '',
          dataPolicy.leader_status === 'market_rank_proxy' ? '<span class="chip warn">시장순위 프록시</span>' : '',
          dataPolicy.minimum_probe ? '<span class="chip warn">최소수량만 허용</span>' : '',
          dataPolicy.add_buy_policy === 'blocked' ? '<span class="chip bad">추가매수 금지</span>' : ''
        ].filter(Boolean).join(' ');
        const replayChart = renderReplayChart(replay, plan);
        const planHtml = plan ?
          `<div class="muted" style="margin-top:5px">계획 ${esc(plan.entry_type)} · 손절 ${money(plan.stop_loss_price)} · 1차익절 ${money(plan.first_take_profit_price)} · 본절 ${money(plan.breakeven_price)}${partialText}${structureText ? `<br>${esc(structureText)}` : ''}${chartText ? `<br>${esc(chartText)}` : ''}${replayText ? `<br>복기 ${esc(replayText)}` : ''}</div>${replayChart}` :
          `${replayText ? `<div class="muted" style="margin-top:5px">복기 ${esc(replayText)}</div>` : ''}${replayChart}`;
        return `<tr><td>${esc(x.source_data_time)}</td><td>${esc(x.strategy_name)} v${esc(x.version)}</td><td>${esc(x.stock_code)}</td><td>${esc(x.action)}</td><td>${Number(x.score).toFixed(1)}</td><td>${esc(x.outcome)}</td><td>${esc(x.order_status || '-')}</td><td title="${esc(x.component_summary)}">${summary}<div class="muted" style="margin-top:5px">${esc(x.reason)}</div>${planHtml}${dataChips ? `<div style="margin-top:5px">${dataChips}</div>` : ''}</td></tr>`;
      }).join('') :
      '<tr><td colspan="8" class="muted">저장된 공통 전략 판단이 없습니다.</td></tr>';
    const real = d.real_account;
    document.getElementById('real-time').textContent = real.snapshot ? real.snapshot.collected_at : '동기화 이력 없음';
    document.getElementById('real-time').className = cls(!!real.snapshot, !real.snapshot);
    document.getElementById('real-deposit').textContent = real.snapshot ? money(real.snapshot.deposit) : '-';
    document.getElementById('real-assets').textContent = real.snapshot ? money(real.snapshot.estimated_assets) : '-';
    const pnl = document.getElementById('real-pnl');
    pnl.textContent = real.snapshot ? money(real.snapshot.total_profit_loss) : '-';
    pnl.className = `metric ${real.snapshot && real.snapshot.total_profit_loss >= 0 ? 'ok' : 'bad'}`;
    document.getElementById('real-positions').innerHTML = real.positions.length ?
      real.positions.map(p => `<tr><td>${esc(p.stock_name || p.stock_code)}</td><td>${Number(p.quantity || 0).toLocaleString()}</td><td>${money(p.current_price)}</td><td>${money(p.purchase_price)}</td><td>${money(p.evaluation_amount)}</td><td class="${(p.evaluation_profit_loss || 0) >= 0 ? 'ok' : 'bad'}">${money(p.evaluation_profit_loss)}</td></tr>`).join('') :
      '<tr><td colspan="6" class="muted">저장된 실전 보유 종목이 없습니다.</td></tr>';
    document.getElementById('real-orders').innerHTML = real.orders.length ?
      real.orders.map(o => `<tr><td>${esc(o.order_time || o.collected_at)}</td><td>${esc(o.stock_name || o.stock_code || '-')}</td><td>${esc(o.trade_type || '-')}</td><td>${Number(o.order_quantity || 0).toLocaleString()}주</td><td>${Number(o.executed_quantity || 0).toLocaleString()}주</td><td>${esc(o.status || '-')}</td></tr>`).join('') :
      '<tr><td colspan="6" class="muted">저장된 실전 주문·체결 내역이 없습니다.</td></tr>';
    document.getElementById('events').innerHTML = d.events.map(e =>
      `<tr><td>${esc(e.occurred_at)}</td><td>${esc(e.module)}</td><td>${esc(e.severity)}</td><td>${esc(e.message)}</td></tr>`).join('');
  } catch (e) {
    document.getElementById('message').textContent = `상태 조회 실패: ${e}`;
  }
}
async function refreshRuntime() {
  try {
    const d = await fetch('/api/runtime-status', {cache:'no-store'}).then(r => r.json());
    const rt = document.getElementById('runtime');
    rt.textContent = d.running ? `실행 중 (PID ${d.pid})` :
      (d.stop_requested ? '종료 대기 중' : '정지');
    rt.className = cls(d.running, d.stop_requested);
  } catch (_) {
    // Keep the last known runtime state while the local dashboard restarts.
  }
}
async function post(path, body={}) {
  const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const data = await response.json();
  document.getElementById('message').textContent = data.message || data.error || '';
  await refresh();
}
function isSmallAccountAggressiveChecked() {
  const checkbox = document.getElementById('small-account-aggressive');
  return checkbox ? checkbox.checked : false;
}
function strategyAbActiveMode() {
  const select = document.getElementById('strategy-ab-active-mode');
  return select ? select.value : 'remake';
}
function startTrader() {
  post('/api/start', {
    interval: Number(document.getElementById('interval').value),
    account: document.getElementById('account').value,
    auto_strategy: document.getElementById('strategy').checked,
    small_account_aggressive: isSmallAccountAggressiveChecked(),
    strategy_ab_active_mode: strategyAbActiveMode(),
    with_depth: document.getElementById('depth').checked,
    with_trades: document.getElementById('trades').checked
  });
}
function startUnattended() {
  if (confirm('상위 후보로 모의투자를 실행하고 15:35에 보고서 저장 후 자동 종료할까요?')) {
    post('/api/start-unattended', {
      interval: Number(document.getElementById('interval').value),
      account: document.getElementById('account').value,
      candidate_limit: 20,
      small_account_aggressive: isSmallAccountAggressiveChecked(),
      strategy_ab_active_mode: strategyAbActiveMode()
    });
  }
}
function startUniverseCollection() {
  post('/api/universe/start', {
    target_bars: Number(document.getElementById('target-bars').value)
  });
}
function stopUniverseCollection() {
  post('/api/universe/stop');
}
function scanCandidates() {
  post('/api/candidates/scan');
}
function runAiAnalysis() {
  post('/api/ai/analyze');
}
function stopTrader() {
  if (confirm('신규 수집과 자동 전략 실행을 정상 종료할까요? 보유 포지션은 즉시 청산되지 않습니다.')) post('/api/stop');
}
const savedTab = localStorage.getItem('dashboard-tab');
showTab(['paper', 'performance', 'theme-trends', 'leader', 'live'].includes(savedTab) ? savedTab : 'paper');
refreshRuntime();
refresh();
setInterval(refreshRuntime, 5000);
setInterval(refresh, 60000);
</script></body></html>"""


class DashboardService:
    def __init__(self, database: Database, runtime: CollectorRuntimeControl) -> None:
        self.database = database
        self.runtime = runtime
        self.universe_runtime = UniverseCollectionRuntime(database.path)
        self.operations = OperationsService(database)
        self.calendar = MarketCalendar(database)
        self._status_lock = threading.Lock()
        self._status_cache: Optional[dict[str, Any]] = None
        self._status_cache_at = 0.0
        risk_settings = load_risk_settings()
        register_breakout_strategy(
            database,
            load_strategy_settings(),
            load_paper_trading_settings(),
            risk_settings,
        )
        register_risk_strategy(database, risk_settings)

    def status(self) -> dict[str, Any]:
        cache_age = time.monotonic() - self._status_cache_at
        if self._status_cache is not None and cache_age < 5:
            return self._status_cache
        with self._status_lock:
            cache_age = time.monotonic() - self._status_cache_at
            if self._status_cache is not None and cache_age < 5:
                return self._status_cache
            payload = self._build_status()
            self._status_cache = payload
            self._status_cache_at = time.monotonic()
            return payload

    def runtime_status(self) -> dict[str, Any]:
        return self.runtime.status()

    def _build_status(self) -> dict[str, Any]:
        now = datetime.now(SEOUL_TIMEZONE)
        runtime = self.runtime.status()
        try:
            settings = load_settings()
            credentials_configured = bool(settings.app_key and settings.secret_key)
            server_mode = (
                "모의 시세 서버"
                if settings.use_mock
                else "실전 시세 서버 · 주문은 모의"
            )
            token_cached = (
                settings.token_cache_path is not None
                and settings.token_cache_path.exists()
            )
        except RuntimeError:
            credentials_configured = False
            server_mode = "설정 안 됨"
            token_cached = False
        latest_at = self._latest_price_time()
        max_age = load_market_data_settings().max_price_age_seconds
        latest_age = None
        if latest_at:
            latest = datetime.fromisoformat(latest_at)
            if latest.tzinfo is None:
                latest = latest.replace(tzinfo=SEOUL_TIMEZONE)
            latest_age = max(0, int((now - latest).total_seconds()))
        fresh = latest_age is not None and latest_age <= max_age
        day = self.calendar.get_day(now.date())
        health = self.operations.health()
        market_mode_gate = self._market_mode_settings()
        market_mode_auto = MarketModeService(self.database).latest_snapshot()
        return {
            "runtime": runtime,
            "api": {
                "credentials_configured": credentials_configured,
                "server_mode": server_mode,
                "token_cached": token_cached,
                "recent_success": fresh,
                "connection_label": "최근 통신 성공" if fresh else "최근 통신 확인 필요",
            },
            "market": {
                "open": self.calendar.is_market_open(now),
                "date": now.date().isoformat(),
                "day_name": day.name or ("거래일" if day.status == "open" else "휴장일"),
            },
            "market_mode_gate": {
                "enabled": market_mode_gate.enabled,
                "manual_mode": market_mode_gate.manual_mode,
                "requested_mode": market_mode_gate.requested_mode,
                "source": market_mode_gate.source,
                "reason": market_mode_gate.reason,
                "entry_state": market_mode_gate.entry_state,
                "entry_allowed": market_mode_gate.entry_allowed,
                "order_effect": market_mode_gate.order_effect,
                "quantity_multiplier": str(market_mode_gate.quantity_multiplier),
                "allowed_entry_grades": market_mode_gate.allowed_entry_grades,
                "shadow_mode": market_mode_gate.shadow_mode,
            },
            "market_mode_auto": market_mode_auto,
            "collection": {
                "latest_at": latest_at,
                "age_seconds": latest_age,
                "fresh": fresh,
                "max_age_seconds": max_age,
            },
            "accounts": self._paper_accounts(),
            "risk_positions": self._paper_risk_positions(),
            "universe": self._universe_status(),
            "automation_flow": self._automation_flow(runtime, now),
            "paper_performance": self._paper_performance(now),
            "candidates": self._candidate_status(),
            "theme_trends": self._theme_trends_status(),
            "leader_themes": self._leader_theme_status(),
            "realtime_rankings": self._realtime_ranking_status(),
            "information": self._information_status(),
            "ai_shadow": self._ai_shadow_status(),
            "strategy": self._strategy_status(runtime),
            "real_account": self._real_account(),
            "alerts": health["active_alerts"],
            "events": self.operations.list_events(limit=12),
        }

    def start(self, body: dict[str, Any]) -> dict[str, Any]:
        account = str(body.get("account", "default")).strip()
        auto_strategy = bool(body.get("auto_strategy", True))
        small_account_aggressive = bool(
            body.get("small_account_aggressive", False)
        )
        strategy_ab_active_mode = str(
            body.get("strategy_ab_active_mode") or "remake"
        ).strip().lower()
        if strategy_ab_active_mode not in {"normal", "remake"}:
            raise ValueError("전략 A/B 실제모드는 normal 또는 remake만 가능합니다.")
        if auto_strategy:
            with self.database.connect() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM paper_accounts WHERE name = ?",
                    (account,),
                ).fetchone()
            if exists is None:
                raise ValueError(
                    f"모의계좌 '{account}'가 없습니다. "
                    f"먼저 python main.py paper init --account {account} 명령으로 생성하세요."
                )
        return self.runtime.start(
            interval=int(body.get("interval", 60)),
            account=account,
            auto_strategy=auto_strategy,
            small_account_aggressive=small_account_aggressive,
            strategy_ab_active_mode=strategy_ab_active_mode,
            with_depth=bool(body.get("with_depth", False)),
            with_trades=bool(body.get("with_trades", False)),
        )

    def start_unattended(self, body: dict[str, Any]) -> dict[str, Any]:
        account = str(body.get("account", "default")).strip()
        candidate_limit = int(body.get("candidate_limit", 20))
        small_account_aggressive = bool(
            body.get("small_account_aggressive", False)
        )
        strategy_ab_active_mode = str(
            body.get("strategy_ab_active_mode") or "remake"
        ).strip().lower()
        if strategy_ab_active_mode not in {"normal", "remake"}:
            raise ValueError("전략 A/B 실제모드는 normal 또는 remake만 가능합니다.")
        self._require_paper_account(account)
        settings = load_settings()
        if not settings.app_key or not settings.secret_key:
            raise ValueError("키움 API 환경변수가 설정되지 않았습니다.")
        run = CandidateScreeningEngine(
            self.database,
            load_candidate_settings(),
        ).scan(limit=candidate_limit)
        if run.selected_count < 1:
            raise ValueError(
                "평가 가능한 후보가 없습니다. 전 종목 일봉 수집을 먼저 실행하세요."
            )
        selected = CandidateScreeningEngine(
            self.database,
            load_candidate_settings(),
        ).latest(selected_only=True)
        if not selected:
            raise ValueError("사전점검에 사용할 후보 종목이 없습니다.")
        bootstrap = self._preflight_market_api(
            settings,
            selected[0]["stock_code"],
        )
        return self.runtime.start(
            interval=int(body.get("interval", 60)),
            account=account,
            auto_strategy=True,
            with_depth=True,
            with_trades=False,
            unattended=True,
            candidate_limit=candidate_limit,
            preopen_scan=True,
            small_account_aggressive=small_account_aggressive,
            strategy_ab_active_mode=strategy_ab_active_mode,
            bootstrap_token=bootstrap["token"],
            bootstrap_expires_dt=bootstrap["expires_dt"],
        )

    def _preflight_market_api(
        self,
        settings: Any,
        stock_code: str,
    ) -> dict[str, str]:
        client = KiwoomRestClient(
            app_key=settings.app_key,
            secret_key=settings.secret_key,
            use_mock=settings.use_mock,
            token_cache=settings.token_cache_path,
            timeout=settings.request_timeout,
            max_retries=0,
            api_budget_manager=ApiBudgetManager(self.database),
        )
        try:
            client.get_intraday_prices(stock_code)
        except Exception as exc:
            server = "모의 시세 서버" if settings.use_mock else "실전 시세 서버"
            raise ValueError(
                f"시작 전 API 점검 실패 ({server}, {stock_code}): {exc}"
            ) from exc
        if not client.token or not client.expires_dt:
            raise ValueError("시작 전 API 점검에서 유효한 토큰을 받지 못했습니다.")
        return {
            "token": client.token,
            "expires_dt": client.expires_dt,
        }

    def _require_paper_account(self, account: str) -> None:
        with self.database.connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM paper_accounts WHERE name = ?",
                (account,),
            ).fetchone()
        if exists is None:
            raise ValueError(
                f"모의계좌 '{account}'가 없습니다. "
                f"먼저 python main.py paper init --account {account} 명령으로 생성하세요."
            )

    def _universe_status(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM stock_universe
                     WHERE tradable = 1) AS registered_count,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END)
                        AS completed_count,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END)
                        AS pending_count,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)
                        AS failed_count,
                    MAX(updated_at) AS updated_at
                FROM universe_collection_status
                """
            ).fetchone()
        return {
            "runtime": self.universe_runtime.status(),
            "registered_count": int(row["registered_count"] or 0),
            "completed_count": int(row["completed_count"] or 0),
            "pending_count": int(row["pending_count"] or 0),
            "failed_count": int(row["failed_count"] or 0),
            "updated_at": row["updated_at"],
        }

    def _latest_price_time(self) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT MAX(collected_at) AS latest_at FROM price_snapshots"
            ).fetchone()
        return row["latest_at"] if row else None

    def _automation_flow(
        self,
        runtime: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        today_start = f"{now.date().isoformat()}T00:00:00"
        with self.database.connect() as connection:
            latest_run = connection.execute(
                """
                SELECT id, run_type, status, started_at, completed_at,
                       success_count, failure_count, message, metadata_json
                FROM operation_runs
                WHERE run_type = 'periodic_collect'
                ORDER BY started_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            signal_counts = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN signal_pass = 1 THEN 1 ELSE 0 END) AS passed,
                       SUM(CASE WHEN action = 'executed' THEN 1 ELSE 0 END) AS executed,
                       SUM(CASE WHEN action = 'failed' THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN action = 'skipped' THEN 1 ELSE 0 END) AS skipped,
                       SUM(CASE WHEN action = 'observe' THEN 1 ELSE 0 END) AS observe,
                       MAX(signal_time) AS latest_signal_time
                FROM strategy_signals
                WHERE signal_time >= ?
                """,
                (today_start,),
            ).fetchone()
            order_counts = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status = 'filled' THEN 1 ELSE 0 END) AS filled,
                       SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) AS rejected,
                       SUM(CASE WHEN side = 'buy' THEN 1 ELSE 0 END) AS buys,
                       SUM(CASE WHEN side = 'sell' THEN 1 ELSE 0 END) AS sells,
                       MAX(completed_at) AS latest_order_time
                FROM paper_orders
                WHERE requested_at >= ?
                """,
                (today_start,),
            ).fetchone()
            recent_signals = connection.execute(
                """
                SELECT s.signal_time, s.stock_code,
                       COALESCE(u.stock_name, w.stock_name) AS stock_name,
                       s.current_price, s.volume_ratio, s.moving_average,
                       s.day_gain_rate, s.volume_pass, s.breakout_pass,
                       s.moving_average_pass, s.chase_pass, s.signal_pass,
                       s.action, s.message
                FROM strategy_signals s
                LEFT JOIN stock_universe u ON u.stock_code = s.stock_code
                LEFT JOIN watchlist w ON w.stock_code = s.stock_code
                WHERE s.signal_time >= ?
                ORDER BY s.signal_time DESC, s.id DESC
                LIMIT 8
                """,
                (today_start,),
            ).fetchall()
            recent_orders = connection.execute(
                """
                SELECT o.requested_at, o.completed_at, o.stock_code,
                       COALESCE(u.stock_name, w.stock_name) AS stock_name,
                       o.side, o.quantity, o.status, o.reason,
                       o.rejection_reason, f.fill_price
                FROM paper_orders o
                LEFT JOIN paper_fills f ON f.order_id = o.id
                LEFT JOIN stock_universe u ON u.stock_code = o.stock_code
                LEFT JOIN watchlist w ON w.stock_code = o.stock_code
                WHERE o.requested_at >= ?
                ORDER BY o.requested_at DESC, o.id DESC
                LIMIT 5
                """,
                (today_start,),
            ).fetchall()
        latest_run_dict = dict(latest_run) if latest_run is not None else None
        if latest_run_dict is not None:
            latest_run_dict["metadata"] = self._parse_json_object(
                latest_run_dict.pop("metadata_json", "{}")
            )
        signals = self._coerce_count_row(
            signal_counts,
            (
                "total",
                "passed",
                "executed",
                "failed",
                "skipped",
                "observe",
            ),
        )
        signals["latest_signal_time"] = (
            signal_counts["latest_signal_time"] if signal_counts else None
        )
        orders = self._coerce_count_row(
            order_counts,
            ("total", "filled", "rejected", "buys", "sells"),
        )
        orders["latest_order_time"] = (
            order_counts["latest_order_time"] if order_counts else None
        )
        return {
            "running": bool(runtime.get("running")),
            "mode_label": self._automation_mode_label(
                runtime,
                latest_run_dict,
                signals.get("latest_signal_time"),
                orders.get("latest_order_time"),
                now,
            ),
            "latest_run": latest_run_dict,
            "signal_counts": signals,
            "order_counts": orders,
            "recent_signals": [dict(row) for row in recent_signals],
            "recent_orders": [dict(row) for row in recent_orders],
        }

    def _paper_performance(self, now: datetime) -> dict[str, Any]:
        today_start = f"{now.date().isoformat()}T00:00:00"
        with self.database.connect() as connection:
            accounts = connection.execute(
                """
                SELECT a.id, a.name, a.initial_cash, a.cash_balance,
                       a.realized_pnl,
                       COALESCE(SUM(p.quantity * ps.current_price), 0) AS market_value,
                       COALESCE(SUM(p.quantity * ps.current_price - p.cost_basis), 0)
                           AS unrealized_pnl,
                       COUNT(p.stock_code) AS position_count
                FROM paper_accounts a
                LEFT JOIN paper_positions p ON p.account_id = a.id
                LEFT JOIN price_snapshots ps ON ps.id = (
                    SELECT id FROM price_snapshots
                    WHERE stock_code = p.stock_code
                    ORDER BY collected_at DESC, id DESC LIMIT 1
                )
                GROUP BY a.id, a.name, a.initial_cash, a.cash_balance,
                         a.realized_pnl
                ORDER BY a.id
                """
            ).fetchall()
            positions = connection.execute(
                """
                SELECT a.name AS account_name, p.stock_code,
                       COALESCE(u.stock_name, w.stock_name) AS stock_name,
                       p.quantity, p.cost_basis,
                       ps.current_price, ps.collected_at AS price_time
                FROM paper_positions p
                JOIN paper_accounts a ON a.id = p.account_id
                LEFT JOIN stock_universe u ON u.stock_code = p.stock_code
                LEFT JOIN watchlist w ON w.stock_code = p.stock_code
                LEFT JOIN price_snapshots ps ON ps.id = (
                    SELECT id FROM price_snapshots
                    WHERE stock_code = p.stock_code
                    ORDER BY collected_at DESC, id DESC LIMIT 1
                )
                ORDER BY a.id, p.stock_code
                """
            ).fetchall()
            fills = connection.execute(
                """
                SELECT f.filled_at, f.stock_code,
                       COALESCE(u.stock_name, w.stock_name) AS stock_name,
                       f.side, f.quantity, f.market_price, f.fill_price,
                       f.gross_amount, f.commission, f.tax,
                       f.slippage_amount, f.realized_pnl,
                       o.reason, d.reason AS decision_reason,
                       tp.entry_type, tp.stop_loss_reason,
                       tp.first_take_profit_reason, tp.trailing_rule,
                       tp.invalidation_reason, tp.setup_json
                FROM paper_fills f
                JOIN paper_orders o ON o.id = f.order_id
                LEFT JOIN order_executions oe ON oe.id = (
                    SELECT id
                    FROM order_executions
                    WHERE internal_order_id = f.order_id
                    ORDER BY id DESC
                    LIMIT 1
                )
                LEFT JOIN order_requests req ON req.id = oe.order_request_id
                LEFT JOIN strategy_decisions d ON d.id = req.decision_id
                LEFT JOIN trade_plans tp ON tp.decision_id = d.id
                LEFT JOIN stock_universe u ON u.stock_code = f.stock_code
                LEFT JOIN watchlist w ON w.stock_code = f.stock_code
                WHERE f.filled_at >= ?
                ORDER BY f.filled_at DESC, f.id DESC
                LIMIT 30
                """,
                (today_start,),
            ).fetchall()
            summary_row = connection.execute(
                """
                SELECT COUNT(*) AS filled_orders,
                       SUM(CASE WHEN f.side = 'buy' THEN 1 ELSE 0 END) AS buy_fills,
                       SUM(CASE WHEN f.side = 'sell' THEN 1 ELSE 0 END) AS sell_fills,
                       SUM(CASE WHEN f.side = 'sell' THEN f.realized_pnl ELSE 0 END)
                           AS realized_pnl,
                       SUM(CASE WHEN f.side = 'sell' AND f.realized_pnl > 0
                                THEN 1 ELSE 0 END) AS winning_trades,
                       SUM(CASE WHEN f.side = 'sell' AND f.realized_pnl < 0
                                THEN 1 ELSE 0 END) AS losing_trades
                FROM paper_fills f
                WHERE f.filled_at >= ?
                """,
                (today_start,),
            ).fetchone()
            last_closed = connection.execute(
                """
                SELECT f.filled_at, f.stock_code,
                       COALESCE(u.stock_name, w.stock_name) AS stock_name,
                       f.quantity, f.fill_price, f.realized_pnl,
                       o.reason
                FROM paper_fills f
                JOIN paper_orders o ON o.id = f.order_id
                LEFT JOIN stock_universe u ON u.stock_code = f.stock_code
                LEFT JOIN watchlist w ON w.stock_code = f.stock_code
                WHERE f.side = 'sell'
                ORDER BY f.filled_at DESC, f.id DESC
                LIMIT 1
                """
            ).fetchone()
        account_rows = []
        total_market_value = 0
        total_unrealized = 0
        for row in accounts:
            item = dict(row)
            item["market_value"] = int(item["market_value"] or 0)
            item["unrealized_pnl"] = int(item["unrealized_pnl"] or 0)
            item["total_equity"] = int(item["cash_balance"]) + item["market_value"]
            total_market_value += item["market_value"]
            total_unrealized += item["unrealized_pnl"]
            account_rows.append(item)
        position_rows = []
        for row in positions:
            item = dict(row)
            current_price = item["current_price"]
            cost_basis = int(item["cost_basis"])
            quantity = int(item["quantity"])
            market_value = (
                int(current_price) * quantity if current_price is not None else None
            )
            unrealized = (
                int(market_value) - cost_basis if market_value is not None else None
            )
            item["average_price"] = cost_basis / quantity if quantity else None
            item["market_value"] = market_value
            item["unrealized_pnl"] = unrealized
            item["return_rate"] = (
                unrealized / cost_basis if unrealized is not None and cost_basis else None
            )
            position_rows.append(item)
        realized = int(summary_row["realized_pnl"] or 0) if summary_row else 0
        closed_trades = int(summary_row["sell_fills"] or 0) if summary_row else 0
        summary = {
            "filled_orders": int(summary_row["filled_orders"] or 0)
            if summary_row
            else 0,
            "buy_fills": int(summary_row["buy_fills"] or 0) if summary_row else 0,
            "sell_fills": closed_trades,
            "closed_trades": closed_trades,
            "winning_trades": int(summary_row["winning_trades"] or 0)
            if summary_row
            else 0,
            "losing_trades": int(summary_row["losing_trades"] or 0)
            if summary_row
            else 0,
            "realized_pnl": realized,
            "unrealized_pnl": total_unrealized,
            "market_value": total_market_value,
            "position_count": len(position_rows),
            "total_pnl": realized + total_unrealized,
        }
        return {
            "summary": summary,
            "accounts": account_rows,
            "positions": position_rows,
            "today_fills": [self._fill_with_reason(dict(row)) for row in fills],
            "last_closed_trade": dict(last_closed) if last_closed else None,
            "type_performance": self._trade_type_performance(),
        }

    @staticmethod
    def _fill_with_reason(row: dict[str, Any]) -> dict[str, Any]:
        setup = DashboardService._parse_json_object(row.pop("setup_json", "{}"))
        reason_parts = []
        if row["side"] == "buy":
            entry_label = {
                "breakout": "돌파",
                "opening_range_breakout": "ORB",
                "pullback_support": "눌림",
                "paper_probe": "모의검증",
            }.get(row.get("entry_type"), row.get("entry_type"))
            if entry_label:
                reason_parts.append(f"{entry_label} 진입")
            if setup.get("pullback_support_pass"):
                reason_parts.append("눌림 지지 확인")
            if setup.get("opening_range_pass"):
                reason_parts.append("장초반 ORB 돌파")
            if setup.get("breakout_hold_pass"):
                reason_parts.append("돌파 유지")
            if setup.get("vwap_pass"):
                reason_parts.append("VWAP 상단")
            if setup.get("order_flow_pass"):
                reason_parts.append("호가·체결 수급 양호")
        else:
            for key in (
                "decision_reason",
                "stop_loss_reason",
                "first_take_profit_reason",
                "trailing_rule",
                "invalidation_reason",
            ):
                value = row.get(key)
                if value and value not in reason_parts:
                    reason_parts.append(str(value))
        fallback = row.get("reason") or row.get("decision_reason") or "-"
        row["display_reason"] = " · ".join(reason_parts[:4]) or fallback
        return row

    def _trade_type_performance(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            plans = connection.execute(
                """
                SELECT tp.id, tp.entry_type, tp.stock_code, tp.entry_price,
                       tp.planned_quantity, tp.stop_loss_price,
                       tp.first_take_profit_price, tp.partial_exit_executed,
                       tp.partial_exit_order_id, tp.partial_exit_at,
                       tp.breakeven_price, d.account_id, d.decided_at,
                       (
                           SELECT MIN(d2.decided_at)
                           FROM trade_plans tp2
                           JOIN strategy_decisions d2
                             ON d2.id = tp2.decision_id
                           WHERE d2.account_id = d.account_id
                             AND tp2.stock_code = tp.stock_code
                             AND d2.action = 'buy'
                             AND (
                                 d2.decided_at > d.decided_at
                                 OR (
                                     d2.decided_at = d.decided_at
                                     AND tp2.id > tp.id
                                 )
                             )
                       ) AS next_plan_at
                FROM trade_plans tp
                JOIN strategy_decisions d ON d.id = tp.decision_id
                WHERE d.action = 'buy'
                  AND d.account_id IS NOT NULL
                ORDER BY d.decided_at, tp.id
                """
            ).fetchall()
            sell_fills = connection.execute(
                """
                SELECT o.account_id, f.order_id, f.stock_code, f.filled_at,
                       f.quantity, f.fill_price, f.realized_pnl
                FROM paper_fills f
                JOIN paper_orders o ON o.id = f.order_id
                WHERE f.side = 'sell'
                ORDER BY f.filled_at, f.id
                """
            ).fetchall()

        fills_by_key: dict[tuple[int, str], list[dict[str, Any]]] = {}
        for fill in sell_fills:
            key = (int(fill["account_id"]), str(fill["stock_code"]))
            fills_by_key.setdefault(key, []).append(dict(fill))

        stats: dict[str, dict[str, Any]] = {}
        for plan in plans:
            entry_type = str(plan["entry_type"])
            stat = stats.setdefault(
                entry_type,
                {
                    "entry_type": entry_type,
                    "trade_count": 0,
                    "closed_trades": 0,
                    "winning_trades": 0,
                    "losing_trades": 0,
                    "one_r_hits": 0,
                    "partial_breakeven_count": 0,
                    "partial_breakeven_success": 0,
                    "total_realized_pnl": 0,
                    "profits": [],
                    "losses": [],
                },
            )
            stat["trade_count"] += 1
            key = (int(plan["account_id"]), str(plan["stock_code"]))
            decided_at = str(plan["decided_at"])
            next_plan_at = (
                str(plan["next_plan_at"]) if plan["next_plan_at"] is not None else None
            )
            plan_fills = [
                fill
                for fill in fills_by_key.get(key, [])
                if str(fill["filled_at"]) >= decided_at
                and (next_plan_at is None or str(fill["filled_at"]) < next_plan_at)
            ]
            realized_pnl = sum(int(fill["realized_pnl"] or 0) for fill in plan_fills)
            if plan_fills:
                stat["closed_trades"] += 1
                stat["total_realized_pnl"] += realized_pnl
                if realized_pnl > 0:
                    stat["winning_trades"] += 1
                    stat["profits"].append(realized_pnl)
                elif realized_pnl < 0:
                    stat["losing_trades"] += 1
                    stat["losses"].append(realized_pnl)

            if int(plan["partial_exit_executed"] or 0):
                stat["one_r_hits"] += 1
                partial_at = plan["partial_exit_at"]
                partial_order_id = plan["partial_exit_order_id"]
                remainder_fills = [
                    fill
                    for fill in plan_fills
                    if partial_at is not None
                    and str(fill["filled_at"]) >= str(partial_at)
                    and (
                        partial_order_id is None
                        or int(fill["order_id"]) != int(partial_order_id)
                    )
                ]
                if remainder_fills:
                    stat["partial_breakeven_count"] += 1
                    remainder_pnl = sum(
                        int(fill["realized_pnl"] or 0) for fill in remainder_fills
                    )
                    remainder_quantity = sum(
                        int(fill["quantity"] or 0) for fill in remainder_fills
                    )
                    average_exit = (
                        sum(
                            int(fill["quantity"] or 0) * int(fill["fill_price"] or 0)
                            for fill in remainder_fills
                        )
                        / remainder_quantity
                        if remainder_quantity
                        else 0
                    )
                    if (
                        remainder_pnl >= 0
                        or average_exit >= int(plan["breakeven_price"])
                    ):
                        stat["partial_breakeven_success"] += 1

        rows = []
        for stat in stats.values():
            closed = int(stat["closed_trades"])
            trade_count = int(stat["trade_count"])
            average_profit = (
                sum(stat["profits"]) / len(stat["profits"])
                if stat["profits"]
                else None
            )
            average_loss = (
                sum(stat["losses"]) / len(stat["losses"])
                if stat["losses"]
                else None
            )
            profit_loss_ratio = (
                average_profit / abs(average_loss)
                if average_profit is not None and average_loss
                else None
            )
            rows.append(
                {
                    "entry_type": stat["entry_type"],
                    "trade_count": trade_count,
                    "closed_trades": closed,
                    "winning_trades": int(stat["winning_trades"]),
                    "losing_trades": int(stat["losing_trades"]),
                    "win_rate": stat["winning_trades"] / closed if closed else None,
                    "average_profit": round(average_profit)
                    if average_profit is not None
                    else None,
                    "average_loss": round(average_loss)
                    if average_loss is not None
                    else None,
                    "profit_loss_ratio": profit_loss_ratio,
                    "expectancy": round(stat["total_realized_pnl"] / closed)
                    if closed
                    else None,
                    "one_r_hit_rate": stat["one_r_hits"] / trade_count
                    if trade_count
                    else None,
                    "partial_breakeven_count": int(
                        stat["partial_breakeven_count"]
                    ),
                    "partial_breakeven_success": int(
                        stat["partial_breakeven_success"]
                    ),
                    "partial_breakeven_success_rate": (
                        stat["partial_breakeven_success"]
                        / stat["partial_breakeven_count"]
                        if stat["partial_breakeven_count"]
                        else None
                    ),
                }
            )
        return sorted(
            rows,
            key=lambda row: (
                -int(row["trade_count"]),
                -(row["expectancy"] if row["expectancy"] is not None else -10**18),
                str(row["entry_type"]),
            ),
        )

    @staticmethod
    def _coerce_count_row(row: Any, keys: tuple[str, ...]) -> dict[str, int]:
        return {key: int(row[key] or 0) if row else 0 for key in keys}

    @staticmethod
    def _parse_json_object(raw: str) -> dict[str, Any]:
        try:
            value = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _parse_json_list(raw: str) -> list[Any]:
        try:
            value = json.loads(raw or "[]")
        except json.JSONDecodeError:
            return []
        return value if isinstance(value, list) else []

    @staticmethod
    def _parse_local_time(raw: Any) -> datetime | None:
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=SEOUL_TIMEZONE)
        return parsed.astimezone(SEOUL_TIMEZONE)

    def _automation_mode_label(
        self,
        runtime: dict[str, Any],
        latest_run: dict[str, Any] | None,
        latest_signal_time: str | None,
        latest_order_time: str | None,
        now: datetime,
    ) -> str:
        if not runtime.get("running"):
            return "정지"
        latest_order = self._parse_local_time(latest_order_time)
        if latest_order and now - latest_order <= timedelta(minutes=5):
            return "최근 모의 주문 발생"
        if latest_run and latest_run.get("status") == "running":
            return "수집·전략 평가 중"
        metadata = runtime.get("metadata") or {}
        interval = int(metadata.get("interval") or 60)
        latest_signal = self._parse_local_time(latest_signal_time)
        if latest_signal and now - latest_signal <= timedelta(
            seconds=max(interval * 2, 90)
        ):
            return "전략 평가 중"
        return "다음 주기 대기"

    def _paper_accounts(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT a.name, a.cash_balance,
                       COALESCE(SUM(p.quantity * ps.current_price), 0) AS market_value,
                       COUNT(p.stock_code) AS position_count
                FROM paper_accounts a
                LEFT JOIN paper_positions p ON p.account_id = a.id
                LEFT JOIN price_snapshots ps ON ps.id = (
                    SELECT id FROM price_snapshots
                    WHERE stock_code = p.stock_code
                    ORDER BY collected_at DESC, id DESC LIMIT 1
                )
                GROUP BY a.id, a.name, a.cash_balance
                ORDER BY a.id
                """
            ).fetchall()
        return [
            {
                **dict(row),
                "total_equity": int(row["cash_balance"]) + int(row["market_value"]),
            }
            for row in rows
        ]

    def _real_account(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            snapshot = connection.execute(
                """
                SELECT id, collected_at, deposit, withdrawable_amount,
                       orderable_cash, total_purchase_amount,
                       total_evaluation_amount, total_profit_loss,
                       total_profit_rate, estimated_assets
                FROM real_account_snapshots
                ORDER BY collected_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            positions = []
            if snapshot is not None:
                positions = connection.execute(
                    """
                    SELECT stock_code, stock_name, quantity, tradable_quantity,
                           current_price, purchase_price, purchase_amount,
                           evaluation_amount, evaluation_profit_loss, profit_rate
                    FROM real_positions
                    WHERE account_snapshot_id = ?
                    ORDER BY stock_code
                    """,
                    (snapshot["id"],),
                ).fetchall()
            orders = connection.execute(
                """
                SELECT collected_at, order_date, order_number, stock_code,
                       stock_name, trade_type, order_type_name, order_quantity,
                       order_price, executed_quantity, executed_price,
                       remaining_quantity, order_time, status,
                       original_order_number, exchange_type
                FROM real_order_execution_snapshots
                ORDER BY collected_at DESC, id DESC
                LIMIT 10
                """
            ).fetchall()
        return {
            "snapshot": dict(snapshot) if snapshot is not None else None,
            "positions": [dict(row) for row in positions],
            "orders": [dict(row) for row in orders],
        }

    def _paper_risk_positions(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT a.name AS account_name, p.stock_code, p.quantity,
                       CAST(p.cost_basis AS REAL) / p.quantity AS average_price,
                       ps.current_price, ps.collected_at AS price_time,
                       r.highest_price, r.initial_stop_price,
                       r.current_stop_price, r.current_take_profit_price,
                       r.trailing_active, r.breakeven_active,
                       r.trend_strength, r.trend_score,
                       r.expected_exit_reason, r.last_rule_reason,
                       r.updated_at AS risk_evaluated_at,
                       tp.setup_json AS trade_plan_setup_json,
                       pep.strategy_name AS partial_exit_strategy,
                       pep.first_exit_rate, pep.first_exit_quantity,
                       pep.residual_quantity, pep.first_target_price,
                       pep.first_exit_completed, pep.breakeven_activated,
                       pep.residual_exit_rule, pep.current_zone,
                       tes.trail_price, tes.exit_condition
                FROM paper_accounts a
                JOIN paper_positions p ON p.account_id = a.id
                LEFT JOIN paper_risk_profiles r
                  ON r.account_id = p.account_id AND r.stock_code = p.stock_code
                LEFT JOIN trade_plans tp ON tp.id = (
                    SELECT tp2.id
                    FROM trade_plans tp2
                    JOIN strategy_decisions d2 ON d2.id = tp2.decision_id
                    WHERE d2.account_id = a.id
                      AND tp2.stock_code = p.stock_code
                      AND d2.action = 'buy'
                    ORDER BY d2.decided_at DESC, tp2.id DESC
                    LIMIT 1
                )
                LEFT JOIN partial_exit_plans pep
                  ON pep.trade_plan_id = tp.id
                LEFT JOIN trailing_exit_state tes
                  ON tes.trade_plan_id = tp.id
                LEFT JOIN price_snapshots ps ON ps.id = (
                    SELECT id
                    FROM price_snapshots
                    WHERE stock_code = p.stock_code
                    ORDER BY collected_at DESC, id DESC
                    LIMIT 1
                )
                ORDER BY a.id, p.stock_code
                """
            ).fetchall()
        positions = []
        for row in rows:
            item = dict(row)
            average_price = item["average_price"]
            current_price = item["current_price"]
            item["average_price"] = (
                int(round(average_price)) if average_price is not None else None
            )
            item["return_rate"] = (
                float(current_price) / float(average_price) - 1
                if current_price is not None and average_price
                else None
            )
            setup = self._parse_json_object(item.pop("trade_plan_setup_json", "{}"))
            item["partial_exit_setup"] = setup
            item["partial_exit_dashboard"] = self._partial_exit_dashboard_state(
                item,
                setup,
            )
            positions.append(item)
        return positions

    def _partial_exit_dashboard_state(
        self,
        position: dict[str, Any],
        setup: dict[str, Any],
    ) -> dict[str, Any]:
        adjustment = setup.get("partial_exit_adjustment") or {}
        residual = setup.get("residual_target_adjustment") or {}
        vwap = setup.get("vwap_exit_state") or {}
        reentry = setup.get("reentry_policy") or {}
        lock = self.database.pending_reentry_lock(str(position["stock_code"]))
        adjusted_target = setup.get("adjusted_second_take_profit_price")
        raised_target = residual.get("raised_target") or adjustment.get("new_target")
        target_raised = (
            adjustment.get("adjustment_type") == "raise_residual_target"
            or raised_target is not None
            or adjusted_target is not None
        )
        second_deferred = bool(
            setup.get("second_take_profit_deferred")
            or residual.get("defer_second_target")
        )
        vwap_state = str(vwap.get("state") or "unknown")
        vwap_label = {
            "shakeout_recovered": "흔들림",
            "actual_breakdown": "실제 이탈",
            "watch": "관찰",
            "holding": "유지",
            "unknown": "미확인",
        }.get(vwap_state, vwap_state)
        lock_active = lock is not None
        return {
            "target_raised": target_raised,
            "raised_target": raised_target or adjusted_target,
            "second_deferred": second_deferred,
            "vwap_state": vwap_state,
            "vwap_label": vwap_label,
            "vwap_reason": vwap.get("reason"),
            "reentry_locked": lock_active,
            "reentry_lock_reason": lock.get("reason") if lock else None,
            "reentry_lock_until": lock.get("lock_until") if lock else None,
            "reentry_policy": reentry,
        }

    def _leader_theme_status(self) -> dict[str, Any]:
        scores = []
        for row in self.database.latest_leader_stock_scores(
            limit=20,
            include_components=False,
        ):
            item = dict(row)
            item["blocked"] = bool(item.get("blocked"))
            item["reasons"] = self._parse_json_list(item.pop("reasons_json", "[]"))
            scores.append(item)
        latest_trade_date = scores[0]["trade_date"] if scores else None
        rank_items = [dict(row) for row in self.database.latest_market_rank_items(limit=80)]
        rank_summary = []
        seen_types = set()
        for item in rank_items:
            rank_type = item.get("rank_type")
            if rank_type in seen_types:
                continue
            seen_types.add(rank_type)
            rank_summary.append(item)
        return {
            "latest_trade_date": latest_trade_date,
            "scores": scores,
            "learning_report": LeaderOutcomeReportService(self.database).report(
                days=30,
                limit=10,
            ),
            "theme_journals": [
                dict(row) for row in self.database.list_theme_journals(days=30)
            ],
            "rank_items": rank_summary,
        }

    def _theme_trends_status(self) -> dict[str, Any]:
        items = self.database.list_theme_trends(days=30, limit=30)
        return {
            "days": 30,
            "items": items,
        }

    def _candidate_status(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            run = connection.execute(
                """
                SELECT id, as_of, universe_count, eligible_count,
                       selected_count, created_at
                FROM candidate_runs
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        rows = []
        if run:
            latest = self._dashboard_candidate_rows(int(run["id"]))
            intraday = IntradayVwapService(self.database).latest()
            for item in latest:
                item["features"] = self._dashboard_candidate_features(
                    item.get("features")
                )
                item["intraday"] = intraday.get(item["stock_code"])
            pattern_state_keys = (
                "cup_handle_state",
                "ascending_triangle_state",
                "bull_flag_state",
                "bull_pennant_state",
                "box_breakout_state",
                "double_bottom_state",
                "double_top_state",
                "inverse_head_shoulders_state",
                "head_shoulders_state",
            )
            pattern_prefixes = tuple(
                key.removesuffix("_state")
                for key in pattern_state_keys
            )
            state_priority = {
                "confirmed": 0,
                "ready": 1,
                "breakout_pending": 2,
            }
            pattern_rows = [
                item
                for item in latest
                if any(
                    item.get("features", {}).get(key)
                    in state_priority
                    for key in pattern_state_keys
                )
            ]
            pattern_rows.sort(
                key=lambda item: (
                    min(
                        (
                            state_priority.get(
                                item.get("features", {}).get(
                                    f"{prefix}_state"
                                ),
                                9,
                            )
                            for prefix in pattern_prefixes
                        ),
                        default=9,
                    ),
                    -max(
                        (
                            float(
                                item.get("features", {}).get(
                                    f"{prefix}_score"
                                )
                                or 0
                            )
                            for prefix in pattern_prefixes
                        ),
                        default=0,
                    ),
                    item["stock_code"],
                )
            )
            included = set()
            for row in [
                *latest[:12],
                *pattern_rows[:18],
            ]:
                if row["stock_code"] in included:
                    continue
                rows.append(row)
                included.add(row["stock_code"])
            decision_ids = [
                int(item["composite_decision_id"])
                for item in rows
                if item.get("composite_decision_id")
            ]
            component_map: dict[int, list[dict[str, Any]]] = {}
            if decision_ids:
                placeholders = ",".join("?" for _ in decision_ids)
                with self.database.connect() as connection:
                    component_rows = connection.execute(
                        f"""
                        SELECT decision_id, name, score, weight,
                               weighted_score, reason
                        FROM score_components
                        WHERE decision_id IN ({placeholders})
                        ORDER BY decision_id, id
                        """,
                        decision_ids,
                    ).fetchall()
                for component in component_rows:
                    component_map.setdefault(
                        int(component["decision_id"]),
                        [],
                    ).append(dict(component))
            for item in rows:
                item["composite_components"] = component_map.get(
                    int(item["composite_decision_id"])
                    if item.get("composite_decision_id")
                    else 0,
                    [],
                )
        return {
            "run": dict(run) if run is not None else None,
            "rows": rows,
            "by_stock_category": (
                self._candidate_stock_category_summary(int(run["id"]))
                if run is not None
                else []
            ),
        }

    def _candidate_stock_category_summary(self, run_id: int) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            fetched = connection.execute(
                """
                SELECT s.stock_code, s.selected, s.excluded_reason,
                       u.stock_name
                FROM candidate_scores s
                LEFT JOIN stock_universe u ON u.stock_code = s.stock_code
                WHERE s.run_id = ?
                """,
                (run_id,),
            ).fetchall()
        groups = {
            key: {
                "category": key,
                "label": label,
                "candidate_count": 0,
                "selected_count": 0,
                "excluded_count": 0,
            }
            for key, label in STOCK_CATEGORY_LABELS.items()
        }
        for row in fetched:
            item = dict(row)
            category = stock_category_payload(
                item.get("stock_code"),
                item.get("stock_name"),
            )
            group = groups[category["key"]]
            group["candidate_count"] += 1
            if item.get("selected"):
                group["selected_count"] += 1
            if item.get("excluded_reason"):
                group["excluded_count"] += 1
        return [groups[key] for key in STOCK_CATEGORY_LABELS]

    def _dashboard_candidate_rows(self, run_id: int) -> list[dict[str, Any]]:
        feature_keys = (
            "cup_handle_state",
            "cup_handle_score",
            "ascending_triangle_state",
            "ascending_triangle_score",
            "bull_flag_state",
            "bull_flag_score",
            "bull_pennant_state",
            "bull_pennant_score",
            "box_breakout_state",
            "box_breakout_score",
            "double_bottom_state",
            "double_bottom_score",
            "double_top_state",
            "double_top_score",
            "inverse_head_shoulders_state",
            "inverse_head_shoulders_score",
            "head_shoulders_state",
            "head_shoulders_score",
            "recent5_high_breakout",
            "recent5_high_near",
            "new_60d_high",
            "resistance_breakout",
            "near_resistance",
            "healthy_gap",
            "gap_overheated",
            "day_gain_overheated",
            "leader_theme_candidate",
            "market_rank",
        )
        feature_columns = ", ".join(
            f"json_extract(s.features_json, '$.{key}') AS feature_{key}"
            for key in feature_keys
        )
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                WITH chosen AS (
                    SELECT id, 0 AS group_order
                    FROM candidate_scores
                    WHERE run_id = ?
                    ORDER BY selected DESC,
                             CASE WHEN rank IS NULL THEN 1 ELSE 0 END,
                             rank, total_score DESC, stock_code
                    LIMIT 12
                ),
                pattern_chosen AS (
                    SELECT id, 1 AS group_order
                    FROM candidate_scores
                    WHERE run_id = ?
                      AND (
                          patterns_json NOT IN ('', '[]')
                          OR features_json LIKE '%state": "confirmed"%'
                          OR features_json LIKE '%state":"confirmed"%'
                          OR features_json LIKE '%state": "ready"%'
                          OR features_json LIKE '%state":"ready"%'
                          OR features_json LIKE '%state": "breakout_pending"%'
                          OR features_json LIKE '%state":"breakout_pending"%'
                      )
                    ORDER BY pattern_score DESC, total_score DESC, stock_code
                    LIMIT 18
                ),
                combined AS (
                    SELECT id, MIN(group_order) AS group_order
                    FROM (
                        SELECT * FROM chosen
                        UNION ALL
                        SELECT * FROM pattern_chosen
                    )
                    GROUP BY id
                )
                SELECT s.id, s.run_id, s.stock_code, s.rank, s.selected,
                       s.excluded_reason, s.total_score, s.technical_score,
                       s.pattern_score, s.relative_strength_score,
                       s.liquidity_score, s.close_price, s.rsi, s.atr,
                       s.relative_strength, s.watched, s.score_change,
                       s.evaluated_at, s.information_score,
                       s.information_blocked, s.composite_score,
                       s.composite_status, s.composite_confidence,
                       s.composite_decision_id, s.information_item_count,
                       s.patterns_json, s.reasons_json,
                       u.stock_name, u.market, u.sector,
                       combined.group_order,
                       {feature_columns}
                FROM combined
                JOIN candidate_scores s ON s.id = combined.id
                LEFT JOIN stock_universe u ON u.stock_code = s.stock_code
                ORDER BY combined.group_order,
                         s.selected DESC,
                         CASE WHEN s.rank IS NULL THEN 1 ELSE 0 END,
                         s.rank, s.total_score DESC, s.stock_code
                """,
                (run_id, run_id),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item.pop("group_order", None)
            item["selected"] = bool(item.get("selected"))
            item["watched"] = bool(item.get("watched"))
            item["information_blocked"] = bool(
                item.get("information_blocked")
            )
            item["stock_category"] = stock_category_payload(
                item.get("stock_code"),
                item.get("stock_name"),
            )
            item["patterns"] = self._parse_json_list(
                item.pop("patterns_json", "[]")
            )
            item["reasons"] = self._parse_json_list(
                item.pop("reasons_json", "[]")
            )
            features = {}
            for key in feature_keys:
                value = item.pop(f"feature_{key}", None)
                if key == "market_rank" and isinstance(value, str):
                    value = self._parse_json_object(value)
                if value is not None:
                    features[key] = value
            item["features"] = features
            items.append(item)
        return items

    def _realtime_ranking_status(self) -> dict[str, Any]:
        settings = load_candidate_settings()
        rows = [
            dict(row)
            for row in self.database.latest_realtime_item_rankings(
                query_type=settings.realtime_rank_query_type,
                limit=20,
            )
        ]
        latest_at = rows[0]["observed_at"] if rows else None
        return {
            "query_type": settings.realtime_rank_query_type,
            "latest_at": latest_at,
            "scoring_effect": "점수 미반영",
            "rows": rows,
        }

    def _information_status(self) -> dict[str, Any]:
        settings = load_information_settings(required=False)
        return {
            "configured": bool(
                settings.naver_client_id
                and settings.naver_client_secret
                and settings.dart_api_key
            ),
            "items": InformationService(
                self.database,
                settings,
            ).recent(limit=50),
        }

    def _ai_shadow_status(self) -> dict[str, Any]:
        settings = load_ai_shadow_settings()
        service = AiShadowService(self.database, settings)
        return {
            "enabled": settings.enabled,
            "configured": service.configured(),
            "provider": settings.provider,
            "model": settings.model,
            "prompt_version": settings.prompt_version,
            "rows": service.recent(limit=30),
        }

    def run_ai_shadow(self) -> dict[str, int]:
        return AiShadowService(
            self.database,
            load_ai_shadow_settings(),
        ).analyze_latest()

    def replay_review(self, query: dict[str, list[str]]) -> dict[str, Any]:
        def first(name: str) -> Optional[str]:
            value = (query.get(name) or [""])[0].strip()
            return value or None

        stock_code = first("stock_code")
        start_date = first("start_date")
        end_date = first("end_date")
        action = first("action")
        block_reason = first("block_reason")
        try:
            limit = int(first("limit") or "50")
        except ValueError as exc:
            raise ValueError("조회 건수는 숫자여야 합니다.") from exc
        limit = max(1, min(limit, 100))
        parsed_start = (
            datetime.fromisoformat(start_date).date() if start_date else None
        )
        parsed_end = datetime.fromisoformat(end_date).date() if end_date else None
        if parsed_start and parsed_end and parsed_start > parsed_end:
            raise ValueError("시작일은 종료일보다 늦을 수 없습니다.")
        if action and action not in {"buy", "hold", "sell"}:
            raise ValueError("판단 필터는 buy, hold, sell만 허용합니다.")
        if stock_code and len(stock_code) > 20:
            raise ValueError("종목코드가 너무 깁니다.")
        if block_reason and len(block_reason) > 200:
            raise ValueError("차단 사유가 너무 깁니다.")
        return self.database.strategy_replay_review(
            limit=limit,
            stock_code=stock_code,
            start_date=start_date,
            end_date=end_date,
            action=action,
            block_reason=block_reason,
        )

    def _strategy_status(self, runtime: dict[str, Any]) -> dict[str, Any]:
        risk = load_risk_settings()
        with self.database.connect() as connection:
            versions = connection.execute(
                """
                SELECT v.id, v.strategy_name, v.version, v.active,
                       v.description,
                       (
                           SELECT c.id
                           FROM strategy_configs c
                           WHERE c.strategy_version_id = v.id AND c.active = 1
                           ORDER BY c.effective_from DESC, c.id DESC
                           LIMIT 1
                       ) AS config_id,
                       (
                           SELECT c.config_json
                           FROM strategy_configs c
                           WHERE c.strategy_version_id = v.id AND c.active = 1
                           ORDER BY c.effective_from DESC, c.id DESC
                           LIMIT 1
                       ) AS config_json
                FROM strategy_versions v
                ORDER BY v.active DESC, v.strategy_name, v.version DESC
                """
            ).fetchall()
            decisions = connection.execute(
                """
                SELECT d.id, d.stock_code, d.source_data_time, d.action,
                       d.score, d.confidence, d.reason, d.outcome, d.message,
                       d.occurrence_count, d.last_seen_at,
                       v.strategy_name, v.version,
                       (
                           SELECT r.status
                           FROM order_requests r
                           WHERE r.decision_id = d.id
                           ORDER BY r.id DESC
                           LIMIT 1
                       ) AS order_status
                FROM strategy_decisions d
                JOIN strategy_versions v ON v.id = d.strategy_version_id
                WHERE v.strategy_name != 'composite'
                ORDER BY d.decided_at DESC, d.id DESC
                LIMIT 12
                """
            ).fetchall()
            decision_ids = [int(row["id"]) for row in decisions]
            components: dict[int, list[str]] = {}
            component_details: dict[int, list[dict[str, Any]]] = {}
            trade_plans: dict[int, dict[str, Any]] = {}
            replay_rows: dict[int, dict[str, Any]] = {}
            if decision_ids:
                placeholders = ", ".join("?" for _ in decision_ids)
                component_rows = connection.execute(
                    f"""
                    SELECT decision_id, name, score, weight, confidence,
                           weighted_score, source, reason
                    FROM score_components
                    WHERE decision_id IN ({placeholders})
                    ORDER BY decision_id, id
                    """,
                    decision_ids,
                ).fetchall()
                for row in component_rows:
                    components.setdefault(int(row["decision_id"]), []).append(
                        f"{row['name']}={float(row['weighted_score']):.1f}"
                    )
                    component_details.setdefault(
                        int(row["decision_id"]),
                        [],
                    ).append(
                        {
                            "name": row["name"],
                            "score": float(row["score"]),
                            "weight": float(row["weight"]),
                            "confidence": float(row["confidence"]),
                            "weighted_score": float(row["weighted_score"]),
                            "source": row["source"],
                            "reason": row["reason"],
                        }
                    )
                plan_rows = connection.execute(
                    f"""
                    SELECT *
                    FROM trade_plans
                    WHERE decision_id IN ({placeholders})
                    ORDER BY decision_id, id
                    """,
                    decision_ids,
                ).fetchall()
                for row in plan_rows:
                    item = dict(row)
                    try:
                        item["entry_tags"] = json.loads(
                            item.pop("entry_tags_json") or "[]"
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        item["entry_tags"] = []
                    try:
                        item["setup"] = json.loads(
                            item.pop("setup_json") or "{}"
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        item["setup"] = {}
                    trade_plans[int(row["decision_id"])] = item
                replay_rows = {}
        recent = []
        for row in decisions:
            item = dict(row)
            item["component_summary"] = ", ".join(
                components.get(int(row["id"]), [])
            )
            item["components"] = component_details.get(int(row["id"]), [])
            item["trade_plan"] = trade_plans.get(int(row["id"]))
            item["replay"] = replay_rows.get(int(row["id"]))
            plan_setup = (item["trade_plan"] or {}).get("setup") or {}
            replay_details = (item["replay"] or {}).get("details") or {}
            item["data_availability_policy"] = (
                plan_setup.get("data_availability_policy")
                or replay_details.get("data_availability_policy")
                or {}
            )
            recent.append(item)
        metadata = runtime.get("metadata") or {}
        versions_with_details = [
            self._strategy_version_details(dict(row)) for row in versions
        ]
        return {
            "runtime_enabled": bool(
                runtime.get("running") and metadata.get("auto_strategy")
            ),
            "small_account_aggressive": bool(
                runtime.get("running")
                and metadata.get("small_account_aggressive")
            ),
            "strategy_ab_active_mode": (
                metadata.get("strategy_ab_active_mode") or "remake"
            ),
            "versions": versions_with_details,
            "risk_settings": {
                "stop_loss_rate": float(risk.stop_loss_rate),
                "take_profit_rate": float(risk.take_profit_rate),
                "take_profit_enabled": risk.take_profit_enabled,
                "trailing_activation_rate": float(
                    risk.trailing_activation_rate
                ),
                "trailing_drawdown_rate": float(risk.trailing_drawdown_rate),
                "atr_period": risk.atr_period,
                "atr_stop_multiple": float(risk.atr_stop_multiple),
                "breakeven_activation_rate": float(
                    risk.breakeven_activation_rate
                ),
                "strong_trend_take_profit_rate": float(
                    risk.strong_trend_take_profit_rate
                ),
                "strong_trend_trailing_drawdown_rate": float(
                    risk.strong_trend_trailing_drawdown_rate
                ),
                "weak_trend_trailing_drawdown_rate": float(
                    risk.weak_trend_trailing_drawdown_rate
                ),
                "daily_max_loss_rate": float(risk.daily_max_loss_rate),
                "max_consecutive_losses": risk.max_consecutive_losses,
                "max_position_rate": float(risk.max_position_rate),
                "max_positions": risk.max_positions,
                "max_entry_gain_rate": float(risk.max_entry_gain_rate),
            },
            "recent_decisions": recent,
            "supply_shadow": self._supply_shadow_status(),
            "supply_pipeline": self._supply_pipeline_status(detailed=False),
            "replay_review": self._dashboard_replay_review(),
            "trading_review": self._trading_review_status(),
            "intraday_mode": self._intraday_mode_status(),
            "order_safety": self._order_safety_status(limit=20),
            "api_budget": self._api_budget_status(limit=20),
            "operation_warning": self._operation_warning_status(limit=20),
        }

    def _trading_review_status(self) -> dict[str, Any]:
        today = datetime.now(SEOUL_TIMEZONE).date()
        return TradingReviewReport(self.database).build(
            today,
            decision_limit=300,
        )

    def _dashboard_replay_review(self) -> Optional[dict[str, Any]]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM strategy_replay_snapshots"
            ).fetchone()
        if row is not None and int(row["count"] or 0) <= 200:
            return self.database.strategy_replay_review(limit=8)
        return None

    def _supply_pipeline_status(
        self,
        *,
        detailed: bool = True,
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            counts = {
                "broker_flow_snapshots": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM broker_flow_snapshots"
                    ).fetchone()[0]
                ),
                "broker_flow_items": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM broker_flow_items"
                    ).fetchone()[0]
                ),
                "foreign_broker_snapshots": int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM broker_flow_snapshots
                        WHERE flow_type = 'foreign_broker_ranking'
                        """
                    ).fetchone()[0]
                ),
                "investor_flow_snapshots": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM investor_flow_snapshots"
                    ).fetchone()[0]
                ),
                "investor_flow_items": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM investor_flow_items"
                    ).fetchone()[0]
                ),
                "program_trade_snapshots": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM program_trade_snapshots"
                    ).fetchone()[0]
                ),
                "supply_scores": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM supply_scores"
                    ).fetchone()[0]
                ),
            }
            latest_score = connection.execute(
                "SELECT MAX(evaluated_at) AS evaluated_at FROM supply_scores"
            ).fetchone()
            latest_event = connection.execute(
                """
                SELECT event_type, severity, message, details_json, occurred_at
                FROM operation_events
                WHERE module = 'leader_supply'
                ORDER BY occurred_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        event = None
        if latest_event is not None:
            event = dict(latest_event)
            try:
                event["details"] = json.loads(event.pop("details_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                event["details"] = {}
        return {
            "counts": counts,
            "latest_supply_score_at": (
                latest_score["evaluated_at"]
                if latest_score is not None
                else None
            ),
            "latest_leader_supply_event": event,
            "program_validation": self._latest_program_validation(),
            "program_outcome_validation": (
                self._program_policy_outcome_validation(limit=100)
                if detailed
                else {
                    "relaxed_decision_count": 0,
                    "relaxed_evaluated_count": 0,
                    "relaxed_rallied_count": 0,
                    "relaxed_dropped_count": 0,
                    "hard_block_decision_count": 0,
                    "hard_block_evaluated_count": 0,
                    "hard_block_helpful_count": 0,
                    "hard_block_missed_rally_count": 0,
                    "relaxed_rows": [],
                    "hard_block_rows": [],
                }
            ),
            "ready": counts["supply_scores"] > 0,
        }

    def _latest_program_validation(self) -> dict[str, Any]:
        rows = self.database.latest_supply_scores(limit=100)
        details = []
        for row in rows:
            try:
                components = json.loads(row["components_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                components = {}
            program = components.get("program") or {}
            if program.get("status") != "collected":
                continue
            details.append(
                {
                    "stock_code": row["stock_code"],
                    "stock_name": row["stock_name"],
                    "state": program.get("policy_state") or "neutral",
                    "hard_block": bool(program.get("hard_block")),
                    "legacy_block_candidate": bool(
                        program.get("legacy_block_candidate")
                    ),
                    "legacy_block_relaxed": bool(
                        program.get("legacy_block_relaxed")
                    ),
                    "pressure": program.get("pressure"),
                    "recent_pressure": program.get("recent_pressure"),
                    "consecutive_net_sell_count": int(
                        program.get("consecutive_net_sell_count") or 0
                    ),
                }
            )
        return {
            "evaluated_at": rows[0]["evaluated_at"] if rows else None,
            "collected_count": len(details),
            "hard_block_count": sum(
                1 for item in details if item["hard_block"]
            ),
            "watch_count": sum(
                1 for item in details if item["state"] == "watch"
            ),
            "positive_count": sum(
                1 for item in details if item["state"] == "positive"
            ),
            "legacy_block_candidate_count": sum(
                1 for item in details if item["legacy_block_candidate"]
            ),
            "legacy_block_relaxed_count": sum(
                1 for item in details if item["legacy_block_relaxed"]
            ),
            "rows": details,
        }

    def _program_policy_outcome_validation(
        self,
        *,
        limit: int = 2000,
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT replay.decision_id, replay.stock_code,
                       replay.evaluated_at, replay.supply_score_json,
                       outcome.horizon_minutes, outcome.max_up_rate,
                       outcome.max_down_rate, outcome.close_rate
                FROM strategy_replay_snapshots replay
                LEFT JOIN decision_forward_outcomes outcome
                  ON outcome.decision_id = replay.decision_id
                 AND outcome.horizon_minutes = (
                     SELECT MAX(latest.horizon_minutes)
                     FROM decision_forward_outcomes latest
                     WHERE latest.decision_id = replay.decision_id
                 )
                WHERE replay.supply_score_json != '{}'
                ORDER BY replay.evaluated_at DESC, replay.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        relaxed = []
        hard_blocked = []
        for row in rows:
            try:
                supply = json.loads(row["supply_score_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            components = supply.get("components") or {}
            program = components.get("program") or {}
            item = {
                "decision_id": row["decision_id"],
                "stock_code": row["stock_code"],
                "evaluated_at": row["evaluated_at"],
                "horizon_minutes": row["horizon_minutes"],
                "max_up_rate": row["max_up_rate"],
                "max_down_rate": row["max_down_rate"],
                "close_rate": row["close_rate"],
            }
            if program.get("legacy_block_relaxed"):
                relaxed.append(item)
            if program.get("hard_block"):
                hard_blocked.append(item)
        relaxed_evaluated = [
            item for item in relaxed if item["horizon_minutes"] is not None
        ]
        hard_evaluated = [
            item
            for item in hard_blocked
            if item["horizon_minutes"] is not None
        ]
        return {
            "relaxed_decision_count": len(relaxed),
            "relaxed_evaluated_count": len(relaxed_evaluated),
            "relaxed_rallied_count": sum(
                1
                for item in relaxed_evaluated
                if float(item["max_up_rate"] or 0) >= 0.03
            ),
            "relaxed_dropped_count": sum(
                1
                for item in relaxed_evaluated
                if float(item["max_down_rate"] or 0) <= -0.02
            ),
            "hard_block_decision_count": len(hard_blocked),
            "hard_block_evaluated_count": len(hard_evaluated),
            "hard_block_helpful_count": sum(
                1
                for item in hard_evaluated
                if float(item["max_down_rate"] or 0) <= -0.02
            ),
            "hard_block_missed_rally_count": sum(
                1
                for item in hard_evaluated
                if float(item["max_up_rate"] or 0) >= 0.03
            ),
            "relaxed_rows": relaxed[:20],
            "hard_block_rows": hard_blocked[:20],
        }

    def _intraday_mode_status(self) -> dict[str, Any]:
        now = datetime.now(SEOUL_TIMEZONE)
        strategy_settings = load_strategy_settings()
        session_profile = (
            session_profile_for(now)
            if strategy_settings.session_profile_enabled
            else ALWAYS_ACTIVE_PROFILE
        )
        market_mode = self._market_mode_settings()
        mode = intraday_strategy_mode_for(
            now,
            session_profile,
            market_mode_enabled=market_mode.enabled,
            market_mode=market_mode.manual_mode,
            market_mode_reason=market_mode.reason,
            market_mode_quantity_multiplier=(
                market_mode.quantity_multiplier
                if market_mode.order_effect == "paper_apply"
                else Decimal("1")
            ),
        )
        return mode.as_dict()

    def _market_mode_settings(self):
        return MarketModeService(self.database).settings_from_base(
            load_market_mode_gate_settings()
        )

    def _order_safety_status(self, *, limit: int = 20) -> dict[str, Any]:
        events = self.database.latest_order_safety_events(limit=limit)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM order_safety_events"
            ).fetchone()
        latest = events[0] if events else None
        return {
            "summary": {
                "total_events": int(row["count"] or 0) if row else 0,
                "recent_failures": sum(1 for event in events if not event["passed"]),
                "window": limit,
                "latest_event_type": latest.get("event_type") if latest else None,
                "latest_at": latest.get("occurred_at") if latest else None,
                "live_order_effect": (
                    "실전 주문 제출 없음 · live 안전장치는 1주/최소금액/"
                    "최신성/중복/미체결/한도 차단 준비"
                ),
            },
            "events": events,
        }

    def _operation_warning_status(self, *, limit: int = 20) -> dict[str, Any]:
        now = datetime.now(SEOUL_TIMEZONE)
        day = now.date().isoformat()
        strategy_settings = load_strategy_settings()
        risk_settings = load_risk_settings()
        market_gate = self._market_mode_settings()
        intraday_mode = self._intraday_mode_status()
        api_budget = self._api_budget_status(limit=limit)["summary"]
        data_quality = api_budget.get("data_quality") or self._api_data_quality_status()
        safety_events = self.database.latest_order_safety_events(limit=limit)
        with self.database.connect() as connection:
            account_row = connection.execute(
                """
                SELECT COALESCE(SUM(initial_cash), 0) AS initial_cash
                FROM paper_accounts
                """
            ).fetchone()
            buy_row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM paper_orders
                WHERE side = 'buy'
                  AND status = 'filled'
                  AND substr(requested_at, 1, 10) = ?
                """,
                (day,),
            ).fetchone()
            realized_row = connection.execute(
                """
                SELECT COALESCE(SUM(f.realized_pnl), 0) AS realized_pnl
                FROM paper_fills f
                JOIN paper_orders o ON o.id = f.order_id
                WHERE f.side = 'sell'
                  AND substr(f.filled_at, 1, 10) = ?
                """,
                (day,),
            ).fetchone()
            stop_row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM paper_risk_events
                WHERE event_type = 'stop_loss'
                  AND status = 'executed'
                  AND substr(occurred_at, 1, 10) = ?
                """,
                (day,),
            ).fetchone()
            bucket_start, bucket_end = self._current_entry_bucket(
                now,
                strategy_settings.entry_bucket_minutes,
            )
            bucket_row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM paper_orders
                WHERE side = 'buy'
                  AND status = 'filled'
                  AND requested_at >= ?
                  AND requested_at < ?
                """,
                (
                    bucket_start.isoformat(timespec="seconds"),
                    bucket_end.isoformat(timespec="seconds"),
                ),
            ).fetchone()
            morning_start = now.replace(
                hour=9,
                minute=0,
                second=0,
                microsecond=0,
            ) + timedelta(minutes=strategy_settings.morning_entry_limit_start_minutes)
            morning_end = now.replace(
                hour=9,
                minute=0,
                second=0,
                microsecond=0,
            ) + timedelta(minutes=strategy_settings.morning_entry_limit_end_minutes)
            morning_row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM paper_orders
                WHERE side = 'buy'
                  AND status = 'filled'
                  AND requested_at >= ?
                  AND requested_at < ?
                """,
                (
                    morning_start.replace(tzinfo=None).isoformat(timespec="seconds"),
                    morning_end.replace(tzinfo=None).isoformat(timespec="seconds"),
                ),
            ).fetchone()

        initial_cash = int(account_row["initial_cash"] or 0) if account_row else 0
        daily_loss_limit = -int(
            Decimal(initial_cash) * risk_settings.daily_max_loss_rate
        )
        realized_pnl = int(realized_row["realized_pnl"] or 0) if realized_row else 0
        buy_count = int(buy_row["count"] or 0) if buy_row else 0
        bucket_buy_count = int(bucket_row["count"] or 0) if bucket_row else 0
        morning_buy_count = int(morning_row["count"] or 0) if morning_row else 0
        stop_loss_count = int(stop_row["count"] or 0) if stop_row else 0
        minutes_since_open = now.hour * 60 + now.minute - 9 * 60
        in_morning_window = (
            strategy_settings.morning_entry_guard_enabled
            and strategy_settings.morning_entry_limit_start_minutes
            <= minutes_since_open
            < strategy_settings.morning_entry_limit_end_minutes
        )

        reasons: list[dict[str, str]] = []
        level = "ok"
        new_buy_allowed = bool(intraday_mode.get("entry_allowed"))
        new_buy_label = "허용"
        new_buy_effect = "일반 신규 BUY 가능"

        if not market_gate.entry_allowed:
            level = "blocked"
            new_buy_allowed = False
            new_buy_label = "차단"
            new_buy_effect = "시장 모드 게이트 차단"
            reasons.append(
                {
                    "code": "market_mode_blocked",
                    "label": "시장 모드 진입 차단",
                    "severity": "error",
                }
            )
        if not intraday_mode.get("entry_allowed"):
            level = "blocked"
            new_buy_allowed = False
            new_buy_label = "차단"
            new_buy_effect = str(intraday_mode.get("reason") or "장중 모드 차단")
            reasons.append(
                {
                    "code": "intraday_mode_blocked",
                    "label": "현재 장중 모드 신규 BUY 차단",
                    "severity": "error",
                }
            )

        loss_limit_hit = daily_loss_limit < 0 and realized_pnl <= daily_loss_limit
        stop_limit_hit = (
            risk_settings.max_daily_stop_losses > 0
            and stop_loss_count >= risk_settings.max_daily_stop_losses
        )
        if loss_limit_hit:
            level = "warn" if level == "ok" else level
            new_buy_label = "권한 축소"
            new_buy_effect = "일반 후보 차단, A급 최소수량만 검토"
            reasons.append(
                {
                    "code": "daily_loss_limit",
                    "label": "일중 손실 컷 도달",
                    "severity": "warning",
                }
            )
        if stop_limit_hit:
            level = "warn" if level == "ok" else level
            new_buy_label = "쿨다운"
            new_buy_effect = "A급 breakout/ORB 최소수량 중심"
            reasons.append(
                {
                    "code": "stop_loss_cooldown",
                    "label": "일중 손절 제한 도달",
                    "severity": "warning",
                }
            )
        morning_limit_hit = (
            in_morning_window
            and strategy_settings.morning_max_new_buys > 0
            and morning_buy_count >= strategy_settings.morning_max_new_buys
        )
        bucket_limit_hit = (
            strategy_settings.max_new_buys_per_bucket > 0
            and bucket_buy_count >= strategy_settings.max_new_buys_per_bucket
        )

        if morning_limit_hit:
            level = "warn" if level == "ok" else level
            new_buy_label = "장초반 슬롯 소진"
            new_buy_effect = "추가 장초반 진입은 A급 슬롯 확인 필요"
            reasons.append(
                {
                    "code": "morning_slots_used",
                    "label": "09:10~09:30 신규 BUY 슬롯 소진",
                    "severity": "warning",
                }
            )
        if bucket_limit_hit:
            level = "warn" if level == "ok" else level
            new_buy_label = "10분 제한 주의"
            new_buy_effect = "같은 10분 구간 추가 BUY 제한 가능"
            reasons.append(
                {
                    "code": "bucket_limit_used",
                    "label": "같은 10분 구간 BUY 제한 근접/도달",
                    "severity": "warning",
                }
            )
        if int(data_quality.get("warning_count") or 0) > 0:
            level = "warn" if level == "ok" else level
            reasons.append(
                {
                    "code": "data_quality_warning",
                    "label": "수급/대장주/시장순위 데이터 공백",
                    "severity": "warning",
                }
            )
        if int(api_budget.get("backoff_groups") or 0) > 0 or int(api_budget.get("recent_429") or 0) > 0:
            level = "warn" if level == "ok" else level
            reasons.append(
                {
                    "code": "api_backoff",
                    "label": "429/backoff 발생",
                    "severity": "warning",
                }
            )

        dry_run_reasons = self._dry_run_reason_summary(safety_events)
        if dry_run_reasons:
            level = "warn" if level == "ok" else level

        labels = {
            "ok": "시작 가능",
            "warn": "주의 필요",
            "blocked": "신규 BUY 차단",
            "bad": "신규 BUY 차단",
        }
        quality_warnings = data_quality.get("warnings") or []
        quality_summary = (
            " · ".join(str(item.get("label") or item.get("code")) for item in quality_warnings[:3])
            if quality_warnings
            else "오늘자 수급/대장주/순위 확인"
        )
        return {
            "level": level,
            "label": labels.get(level, "주의 필요"),
            "date": day,
            "today_new_buys": {
                "count": buy_count,
                "limit_label": (
                    f"장초반 {strategy_settings.morning_max_new_buys}, "
                    f"10분 {strategy_settings.max_new_buys_per_bucket}"
                ),
                "scope": (
                    f"현재 10분 {bucket_buy_count}, "
                    f"장초반 {morning_buy_count}"
                ),
                "warning": (
                    bucket_limit_hit
                    or morning_limit_hit
                ),
                "blocked": False,
            },
            "stop_losses": {
                "count": stop_loss_count,
                "limit": risk_settings.max_daily_stop_losses,
                "warning": stop_loss_count > 0,
                "blocked": stop_limit_hit,
            },
            "daily_pnl": {
                "realized": realized_pnl,
                "loss_limit": daily_loss_limit,
                "initial_cash": initial_cash,
                "warning": realized_pnl < 0,
                "blocked": loss_limit_hit,
            },
            "data_quality": {
                "warning_count": int(data_quality.get("warning_count") or 0),
                "summary": quality_summary,
                "warnings": quality_warnings,
            },
            "api_budget": {
                "backoff_groups": int(api_budget.get("backoff_groups") or 0),
                "recent_429": int(api_budget.get("recent_429") or 0),
                "recent_budget_skipped": int(api_budget.get("recent_budget_skipped") or 0),
                "recent_backoff_blocked": int(api_budget.get("recent_backoff_blocked") or 0),
            },
            "strategy_mode": {
                "name": intraday_mode.get("name"),
                "label": intraday_mode.get("label"),
                "entry_allowed": bool(intraday_mode.get("entry_allowed")),
                "reason": intraday_mode.get("reason"),
            },
            "new_buy": {
                "allowed": new_buy_allowed,
                "label": new_buy_label,
                "effect": new_buy_effect,
            },
            "dry_run": {
                "count": sum(1 for event in safety_events if not event.get("passed")),
                "reasons": dry_run_reasons,
            },
            "reasons": reasons,
        }

    @staticmethod
    def _current_entry_bucket(
        now: datetime,
        bucket_minutes: int,
    ) -> tuple[datetime, datetime]:
        local = now.astimezone(SEOUL_TIMEZONE).replace(tzinfo=None)
        bucket_minutes = max(1, int(bucket_minutes))
        total_minutes = local.hour * 60 + local.minute
        bucket_start_minute = total_minutes // bucket_minutes * bucket_minutes
        start = local.replace(
            hour=bucket_start_minute // 60,
            minute=bucket_start_minute % 60,
            second=0,
            microsecond=0,
        )
        return start, start + timedelta(minutes=bucket_minutes)

    @staticmethod
    def _dry_run_reason_summary(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        counts: dict[tuple[str, str], int] = {}
        for event in events:
            if event.get("passed"):
                continue
            mode = str(event.get("execution_mode") or "-")
            if mode not in {"dry_run", "paper"}:
                continue
            reason = str(event.get("reason") or event.get("event_type") or "-")
            key = (str(event.get("event_type") or "-"), reason)
            counts[key] = counts.get(key, 0) + 1
        return [
            {"event_type": event_type, "reason": reason, "count": count}
            for (event_type, reason), count in sorted(
                counts.items(),
                key=lambda item: (-item[1], item[0][0], item[0][1]),
            )[:5]
        ]

    def _api_budget_status(self, *, limit: int = 20) -> dict[str, Any]:
        windows = self.database.api_budget_windows_status()
        events = self.database.latest_api_budget_events(limit=limit)
        budget_skipped = sum(
            1 for event in events if str(event.get("reason") or "").startswith("skipped_by_budget")
        )
        backoff_blocked = sum(
            1 for event in events if event.get("reason") == "blocked_by_backoff"
        )
        return {
            "summary": {
                "window_count": len(windows),
                "backoff_groups": sum(
                    1 for window in windows if window.get("backoff_until")
                ),
                "backoff_apis": [
                    {
                        "api_group": window.get("api_group"),
                        "api_id": window.get("api_id"),
                        "backoff_until": window.get("backoff_until"),
                    }
                    for window in windows
                    if window.get("backoff_until")
                ],
                "recent_blocked": sum(1 for event in events if event.get("blocked")),
                "recent_budget_skipped": budget_skipped,
                "recent_backoff_blocked": backoff_blocked,
                "recent_429": sum(
                    1 for event in events if int(event.get("status_code") or 0) == 429
                ),
                "window": limit,
                "current_priority_mode": self._api_priority_mode(),
                "data_quality": self._api_data_quality_status(),
            },
            "windows": windows,
            "events": events,
        }

    def _api_data_quality_status(self) -> dict[str, Any]:
        today = datetime.now(SEOUL_TIMEZONE).date().isoformat()
        with self.database.connect() as connection:
            leader = connection.execute(
                f"""
                {Database.leader_stock_scores_latest_cte()}
                SELECT COUNT(*) AS count, MAX(scored_at) AS latest_at
                FROM latest_leader_stock_scores
                WHERE trade_date = ?
                """,
                (today,),
            ).fetchone()
            supply = connection.execute(
                """
                SELECT COUNT(*) AS count, MAX(evaluated_at) AS latest_at
                FROM supply_scores
                WHERE substr(evaluated_at, 1, 10) = ?
                """,
                (today,),
            ).fetchone()
            market_rank = connection.execute(
                """
                SELECT COUNT(*) AS count, MAX(s.observed_at) AS latest_at
                FROM market_rank_items i
                JOIN market_rank_snapshots s ON s.id = i.snapshot_id
                WHERE i.rank_type = 'trade_amount'
                  AND substr(s.observed_at, 1, 10) = ?
                """,
                (today,),
            ).fetchone()
            realtime_rank = connection.execute(
                """
                SELECT COUNT(*) AS count, MAX(observed_at) AS latest_at
                FROM realtime_item_rankings
                WHERE substr(observed_at, 1, 10) = ?
                """,
                (today,),
            ).fetchone()
        items = {
            "leader_scores": dict(leader) if leader is not None else {},
            "supply_scores": dict(supply) if supply is not None else {},
            "market_rank_0187": dict(market_rank) if market_rank is not None else {},
            "realtime_rank_0198": (
                dict(realtime_rank) if realtime_rank is not None else {}
            ),
        }
        labels = {
            "leader_scores": "대장주 점수 없음",
            "supply_scores": "수급 점수 없음",
            "market_rank_0187": "0187 최신 없음",
            "realtime_rank_0198": "0198 최신 없음",
        }
        warnings = [
            {"code": key, "label": labels[key], "severity": "warning"}
            for key, item in items.items()
            if int(item.get("count") or 0) <= 0
        ]
        return {
            "date": today,
            "items": items,
            "warnings": warnings,
            "warning_count": len(warnings),
        }

    @staticmethod
    def _api_priority_mode() -> dict[str, str]:
        now = datetime.now(SEOUL_TIMEZONE)
        local_time = now.time().replace(tzinfo=None)
        if now.weekday() < 5 and local_time.hour == 9 and local_time.minute < 10:
            return {
                "label": "장초반 관찰",
                "description": (
                    "토큰·보유종목·현재 보유 포지션 가격을 최우선으로 두고 "
                    "상세 호가·체결강도는 진입권 후보로 제한합니다."
                ),
            }
        if now.weekday() < 5 and local_time.hour < 15:
            return {
                "label": "장중 우선순위",
                "description": (
                    "토큰·보유종목·0187 시장순위·대장주 TOP 후보·진입 직전 후보를 "
                    "우선하고, 호가·체결강도 상세는 상위 후보 중심으로 수집합니다."
                ),
            }
        return {
            "label": "기본 보호",
            "description": (
                "정규장 핵심 수집을 보호하는 예산 정책을 유지하며 낮은 우선순위 "
                "상세 API는 예산 부족이나 backoff 때 먼저 차단됩니다."
            ),
        }

    def _supply_shadow_status(self) -> dict[str, Any]:
        rows = []
        for row in self.database.latest_supply_scores(
            limit=20,
            include_components=False,
        ):
            item = dict(row)
            try:
                reasons = json.loads(item.pop("reasons_json") or "[]")
            except (TypeError, json.JSONDecodeError):
                reasons = []
            item["reasons"] = reasons
            item["blocked"] = bool(item.get("blocked"))
            item["program_status"] = "not_collected"
            item["program_policy_state"] = "not_collected"
            item["program_legacy_block_relaxed"] = False
            rows.append(item)
        return {
            "policy": {
                "label": "Shadow",
                "storage": "supply_scores 저장",
                "dashboard_effect": "대시보드 표시",
                "live_order_effect": "실전 주문 영향 없음",
                "paper_order_effect": "SQLite 모의 신규 진입 차단 가능",
            },
            "rows": rows,
        }

    @staticmethod
    def _dashboard_candidate_features(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        keys = {
            "cup_handle_state",
            "cup_handle_score",
            "ascending_triangle_state",
            "ascending_triangle_score",
            "bull_flag_state",
            "bull_flag_score",
            "bull_pennant_state",
            "bull_pennant_score",
            "box_breakout_state",
            "box_breakout_score",
            "double_bottom_state",
            "double_bottom_score",
            "double_top_state",
            "double_top_score",
            "inverse_head_shoulders_state",
            "inverse_head_shoulders_score",
            "head_shoulders_state",
            "head_shoulders_score",
            "recent5_high_breakout",
            "recent5_high_near",
            "new_60d_high",
            "resistance_breakout",
            "near_resistance",
            "healthy_gap",
            "gap_overheated",
            "day_gain_overheated",
            "leader_theme_candidate",
            "market_rank",
        }
        return {key: value.get(key) for key in keys if key in value}

    @staticmethod
    def _strategy_version_details(row: dict[str, Any]) -> dict[str, Any]:
        config = DashboardService._parse_strategy_config(row.get("config_json"))
        row.pop("config_json", None)
        name = row.get("strategy_name")
        row["summary"] = DashboardService._strategy_summary(name, config)
        row["details"] = DashboardService._strategy_details(name, config)
        return row

    @staticmethod
    def _parse_strategy_config(raw: Any) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _strategy_summary(name: Any, config: dict[str, Any]) -> str:
        if name == "breakout":
            strategy = config.get("strategy") or {}
            volume = strategy.get("volume_multiple", 1.3)
            trade_amount = int(
                strategy.get("minimum_projected_trade_amount", 50_000_000_000)
            )
            gain = float(strategy.get("max_day_gain_rate", 0.12)) * 100
            lookback = strategy.get("intraday_breakout_lookback", 20)
            orb_minutes = strategy.get("opening_range_minutes", 10)
            probe = strategy.get("paper_probe_enabled", True)
            probe_score = strategy.get("paper_probe_min_score", 50)
            probe_text = (
                f", 모의 검증 {probe_score}점 이상 보조 진입"
                if probe
                else ""
            )
            return (
                f"모의 매수 진입: 예상거래량 {float(volume):.1f}배, "
                f"예상거래대금 {trade_amount // 100_000_000:,}억원 이상, "
                f"VWAP 위, 전일고가/최근 {lookback}봉 또는 장초반 {orb_minutes}분 범위 돌파, "
                f"추격 {gain:.0f}% 이하{probe_text}"
            )
        if name == "composite":
            composite = config.get("composite") or {}
            mode = config.get("mode", "shadow")
            buy = composite.get("buy_threshold", 70)
            observe = composite.get("observe_threshold", 55)
            return f"후보 종합점수 {mode} 모드: 매수후보 {buy}점, 관찰 {observe}점"
        if name == "risk":
            risk = config.get("risk") or {}
            stop = float(risk.get("stop_loss_rate", 0.02)) * 100
            if risk.get("take_profit_enabled", True):
                take = float(risk.get("take_profit_rate", 0.04)) * 100
                take_text = f"익절 {take:.1f}%"
            else:
                take_text = "고정 익절 비활성"
            return f"모의 포지션 청산: 손절 {stop:.1f}%, {take_text}, 트레일링"
        return "등록된 전략 설정"

    @staticmethod
    def _strategy_details(
        name: Any,
        config: dict[str, Any],
    ) -> list[dict[str, str]]:
        if name == "breakout":
            strategy = config.get("strategy") or {}
            risk = config.get("risk") or {}
            volume = float(strategy.get("volume_multiple", 1.3))
            trade_amount = int(
                strategy.get("minimum_projected_trade_amount", 50_000_000_000)
            )
            gain = float(strategy.get("max_day_gain_rate", 0.12)) * 100
            ma_period = strategy.get("moving_average_period", 20)
            interval = strategy.get("minute_interval", 5)
            lookback = strategy.get("intraday_breakout_lookback", 20)
            orb_minutes = strategy.get("opening_range_minutes", 10)
            orb_entry_end = strategy.get("opening_range_entry_end_minutes", 60)
            orb_buffer = (
                float(strategy.get("opening_range_breakout_buffer_rate", 0.002))
                * 100
            )
            probe = strategy.get("paper_probe_enabled", True)
            probe_score = strategy.get("paper_probe_min_score", 50)
            position = float(risk.get("max_position_rate", 1.0)) * 100
            max_positions = risk.get("max_positions", 10)
            details = [
                {"label": "주문 영향", "value": "모의계좌 신규 매수"},
                {"label": "거래량", "value": f"장중 예상거래량 {volume:.1f}배 이상"},
                {
                    "label": "거래대금",
                    "value": f"장중 예상거래대금 {trade_amount // 100_000_000:,}억원 이상",
                },
                {"label": "시간대", "value": "09:00~11:00, 14:00~15:00 가점"},
                {"label": "돌파", "value": f"전일고가 또는 최근 {lookback}개 5분봉 고점 초과"},
                {
                    "label": "장초반 ORB",
                    "value": (
                        f"09:00 이후 {orb_minutes}분 범위 상단 +{orb_buffer:.1f}% 돌파, "
                        f"{orb_entry_end}분 이내 모의 검증"
                    ),
                },
                {"label": "VWAP", "value": "현재가가 당일 세션 VWAP 위"},
                {"label": "이동평균", "value": f"{interval}분봉 {ma_period}개 평균 위"},
                {"label": "추격 제한", "value": f"직전 종가 대비 +{gain:.0f}% 이하"},
                {"label": "비중 제한", "value": f"점수별 10~{position:.0f}%, 최대 {max_positions}종목"},
            ]
            if probe:
                details.insert(
                    1,
                    {
                        "label": "모의 검증 진입",
                        "value": (
                            f"{probe_score}점 이상이고 돌파·ORB·눌림·수급 조건 충족 시 "
                            "모의계좌에서만 보조 매수"
                        ),
                    },
                )
            return details
        if name == "composite":
            composite = config.get("composite") or {}
            return [
                {"label": "주문 영향", "value": "없음, 후보 관찰용"},
                {"label": "매수 후보", "value": f"{composite.get('buy_threshold', 70)}점 이상"},
                {"label": "관찰", "value": f"{composite.get('observe_threshold', 55)}점 이상"},
                {
                    "label": "가중치",
                    "value": (
                        f"기술 {composite.get('technical_weight', 0.20)}, "
                        f"패턴 {composite.get('pattern_weight', 0.25)}, "
                        f"뉴스 {composite.get('information_weight', 0.15)}"
                    ),
                },
            ]
        if name == "risk":
            risk = config.get("risk") or {}
            take_profit_enabled = risk.get("take_profit_enabled", True)
            return [
                {"label": "주문 영향", "value": "모의계좌 보유 종목 청산"},
                {
                    "label": "손절 / 익절",
                    "value": (
                        f"{float(risk.get('stop_loss_rate', 0.02)) * 100:.1f}% / "
                        + (
                            f"+{float(risk.get('take_profit_rate', 0.04)) * 100:.1f}%"
                            if take_profit_enabled
                            else "고정 익절 비활성"
                        )
                    ),
                },
                {
                    "label": "트레일링",
                    "value": (
                        f"+{float(risk.get('trailing_activation_rate', 0.03)) * 100:.1f}%부터, "
                        f"하락폭 {float(risk.get('trailing_drawdown_rate', 0.015)) * 100:.1f}%"
                    ),
                },
                {
                    "label": "ATR 보호",
                    "value": (
                        f"{risk.get('atr_period', 14)}봉 x "
                        f"{float(risk.get('atr_stop_multiple', 2.0)):.1f}"
                    ),
                },
            ]
        return [{"label": "설정", "value": "상세 요약 없음"}]


def make_handler(service: DashboardService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/":
                self._send(HTTPStatus.OK, HTML, "text/html; charset=utf-8")
                return
            if path == "/api/status":
                self._json(HTTPStatus.OK, service.status())
                return
            if path == "/api/runtime-status":
                self._json(HTTPStatus.OK, service.runtime_status())
                return
            if path == "/api/replay-review":
                try:
                    payload = service.replay_review(parse_qs(parsed.query))
                except ValueError as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                self._json(HTTPStatus.OK, payload)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "경로를 찾을 수 없습니다."})

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                body = self._read_json()
                if path == "/api/start":
                    metadata = service.start(body)
                    self._json(
                        HTTPStatus.ACCEPTED,
                        {"message": f"자동매매 시작 요청 완료 (PID {metadata['pid']})"},
                    )
                    return
                if path == "/api/candidates/scan":
                    run = CandidateScreeningEngine(
                        service.database,
                        load_candidate_settings(),
                    ).scan()
                    self._json(
                        HTTPStatus.OK,
                        {
                            "message": (
                                f"후보 계산 완료: 전체 {run.universe_count}, "
                                f"적격 {run.eligible_count}, 선정 {run.selected_count}"
                            ),
                            "run_id": run.run_id,
                        },
                    )
                    return
                if path == "/api/ai/analyze":
                    result = service.run_ai_shadow()
                    self._json(
                        HTTPStatus.OK,
                        {
                            "message": (
                                "AI shadow 분석 완료: "
                                f"성공 {result['success']}, 실패 {result['failed']}"
                            ),
                            **result,
                        },
                    )
                    return
                if path == "/api/start-unattended":
                    metadata = service.start_unattended(body)
                    self._json(
                        HTTPStatus.ACCEPTED,
                        {
                            "message": (
                                f"무인 모의투자 시작 완료 (PID {metadata['pid']})"
                            )
                        },
                    )
                    return
                if path == "/api/universe/start":
                    metadata = service.universe_runtime.start(
                        target_bars=int(body.get("target_bars", 200)),
                    )
                    self._json(
                        HTTPStatus.ACCEPTED,
                        {
                            "message": (
                                f"전 종목 등록·일봉 수집 시작 (PID {metadata['pid']})"
                            )
                        },
                    )
                    return
                if path == "/api/universe/stop":
                    requested = service.universe_runtime.request_stop()
                    self._json(
                        HTTPStatus.OK,
                        {
                            "message": (
                                "전 종목 일봉 수집 중지를 요청했습니다."
                                if requested
                                else "실행 중인 전 종목 수집이 없습니다."
                            )
                        },
                    )
                    return
                if path == "/api/stop":
                    requested = service.runtime.request_stop()
                    message = (
                        "정상 종료를 요청했습니다. 현재 작업 완료 후 종료됩니다."
                        if requested
                        else "실행 중인 자동매매가 없습니다."
                    )
                    self._json(HTTPStatus.OK, {"message": message})
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "경로를 찾을 수 없습니다."})
            except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception:
                self._json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "요청 처리 중 오류가 발생했습니다."},
                )

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 16_384:
                raise ValueError("요청이 너무 큽니다.")
            if length == 0:
                return {}
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("JSON 객체가 필요합니다.")
            return value

        def _json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
            self._send(
                status,
                json.dumps(value, ensure_ascii=False, default=str),
                "application/json; charset=utf-8",
            )

        def _send(self, status: HTTPStatus, body: str, content_type: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self' 'unsafe-inline'",
            )
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def run_dashboard(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("대시보드는 보안을 위해 로컬 주소에서만 실행할 수 있습니다.")
    if not 1 <= port <= 65535:
        raise ValueError("포트는 1~65535 범위여야 합니다.")

    database_path = Path(load_database_path())
    database = Database(database_path)
    database.initialize()
    service = DashboardService(database, CollectorRuntimeControl(database_path))
    server = ThreadingHTTPServer((host, port), make_handler(service))
    url = f"http://{host}:{port}"
    print(f"대시보드 실행 중: {url}")
    print("종료: Ctrl+C")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
