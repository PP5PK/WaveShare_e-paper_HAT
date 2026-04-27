#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
e-paper_monitor.py — Monitor XLX para e-Paper 2.13" B/W (HAT+)
===============================================================
Exibe em tempo real informações do refletor XLX:
  · Últimas 4 transmissões completas (Opening→Closing stream) com duração
  · Clientes de rádio online (excluindo peers XLX/interlinks)
  · Relógio atualizado a cada segundo via partial refresh

Display  : WaveShare 2.13" e-Paper HAT+ (epd2in13_V4)  250 × 122 px
Log XLX  : /var/log/xlx.log
Autor    : PP5KX

Arquitetura de threads
──────────────────────
  Thread log    — relê o log a cada LOG_INTERVAL segundos e extrai eventos
  Thread main   — acorda no próximo segundo exato, renderiza via displayPartial

Ciclos de refresh
─────────────────
  Parcial  (1 s)              — displayPartial() · relógio + last heard (~0,3 s)
  Limpeza  (CLEAN_INTERVAL s) — epd.Clear() + full refresh (anti-ghosting)
  Inversão (INVERT_INTERVAL s)— alterna fundo B/W para evitar marcação permanente

Eventos relevantes do log xlxd
───────────────────────────────
  Conectando : "New client PP5KX   A at IP added with protocol DCS on module D"
  Desconect. : "Client PP5KX   A at IP removed with protocol DCS on module D"
  TX início  : "Opening stream on module D for client PP5KX   A with sid 58692"
  TX fim     : "Closing stream of module D"
  Peer/interlink: protocol XLX → excluído da contagem de clientes de rádio

Uso
───
  python3 e-paper_monitor.py              # loop contínuo
  python3 e-paper_monitor.py --black      # fundo preto, texto branco
  python3 e-paper_monitor.py --simulate   # salva /tmp/epd_preview.png a cada 1 s
  python3 e-paper_monitor.py --once       # full refresh único e encerra
"""

import sys
import os
import re
import time
import logging
import socket
import argparse
import threading
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

# =============================================================================
#  CONFIGURAÇÃO
# =============================================================================

CALLSIGN        = "PP5KX"             # Indicativo do operador
REFLECTOR_NAME  = "XLXBRA"           # Nome exibido no cabeçalho
XLX_LOG         = "/var/log/xlx.log" # Caminho do log do xlxd
LOG_TAIL_LINES  = 1000                # Linhas do final do log a analisar
MAX_LASTHEARD   = 4                   # Entradas de last heard exibidas

LOG_INTERVAL    = 5                   # Segundos entre releituras do log
CLEAN_INTERVAL  = 600                 # Segundos entre limpezas anti-ghosting
INVERT_INTERVAL = 1800                # Segundos entre inversões automáticas de cor

# Caminhos da biblioteca WaveShare (mesmo diretório do script)
_BASE      = os.path.dirname(os.path.abspath(__file__))
EPD_LIBDIR = os.path.join(_BASE, 'waveshare_epd')
EPD_PICDIR = os.path.join(_BASE, 'pic')

FONT_CANDIDATES = [
    os.path.join(EPD_PICDIR, 'Font.ttc'),
    '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',
    '/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf',
    '/usr/share/fonts/truetype/freefont/FreeMono.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
]

DIAS  = ['Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb', 'Dom']
MESES = ['', 'Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun',
          'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez']

# =============================================================================
#  LAYOUT  250 × 122 px
# =============================================================================

W, H         = 250, 122
WHITE, BLACK = 255, 0

Y_SEP1   = 15    # separador após cabeçalho
Y_LH0    = 18    # primeira linha last heard
LH_STEP  = 13    # espaçamento entre linhas
Y_SEP2   = 71    # separador após last heard  (18 + 4×13 = 70 → +1)
Y_ONLINE = 74    # linha de clientes online
Y_SEP3   = 87    # separador antes do rodapé
Y_FOOT   = 91    # rodapé

# Colunas da tabela last heard (x em pixels)
COL_CS    = 2    # callsign
COL_MOD   = 88   # [módulo]
COL_PROTO = 108  # protocolo
COL_HORA  = 152  # HH:MM:SS
COL_DUR   = 218  # duração


# =============================================================================
#  UTILITÁRIOS
# =============================================================================

def carregar_fonte(tamanho: int) -> ImageFont.FreeTypeFont:
    for caminho in FONT_CANDIDATES:
        if os.path.exists(caminho):
            try:
                return ImageFont.truetype(caminho, tamanho)
            except Exception:
                continue
    return ImageFont.load_default()


def largura_texto(draw: ImageDraw.Draw, texto: str, fonte) -> int:
    bbox = draw.textbbox((0, 0), texto, font=fonte)
    return bbox[2] - bbox[0]


def get_ip_principal() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
    except OSError:
        pass
    try:
        import psutil
        for iface in ('eth0', 'eth1', 'wlan0'):
            addrs = psutil.net_if_addrs().get(iface, [])
            for a in addrs:
                if a.family == socket.AF_INET:
                    return a.address
    except Exception:
        pass
    return 'N/A'


def sleep_ate_proximo_segundo() -> None:
    frac = time.time() % 1.0
    time.sleep(1.0 - frac)


def hms_para_seg(t: str) -> int:
    """'HH:MM:SS' → total de segundos desde meia-noite."""
    try:
        h, m, s = t.split(':')
        return int(h) * 3600 + int(m) * 60 + int(s)
    except Exception:
        return 0


def formatar_duracao(seg: int) -> str:
    """Formata duração em segundos para exibição compacta."""
    if seg < 0:
        return '?'
    if seg < 60:
        return f"{seg}s"
    return f"{seg // 60}m{seg % 60:02d}s"


# =============================================================================
#  PARSER DO LOG XLX
# =============================================================================
#
#  Formato das linhas relevantes:
#
#  Conectando (radio):
#    "26 Apr, 12:11:28: New client PP5KX   A at 172.23.127.1 added with protocol DCS on module D"
#
#  Desconectando (radio):
#    "26 Apr, 12:58:45: Client PP5KX   A at 172.23.127.1 removed with protocol DCS on module D"
#
#  Peer/interlink (excluído da contagem):
#    "26 Apr, 12:11:32: New client ECHO     at 127.0.0.1 added with protocol XLX on module E"
#
#  TX início:
#    "26 Apr, 12:44:59: Opening stream on module D for client PP5KX   A with sid 58692"
#
#  TX fim:
#    "26 Apr, 12:45:02: Closing stream of module D"
#
#  Nota: callsign e sufixo vêm separados por espaços: "PP5KX   A"
#        O sufixo é uma letra maiúscula opcional (A, B, C...) ou ausente (peers).
#        O padrão usa [A-Z]? seguido de look-ahead para garantir que não
#        captura palavras como "at" ou "with" como sufixo.

_TS   = r'(\d{1,2} \w+, \d{2}:\d{2}:\d{2})'
_CALL = r'(\S+)\s+([A-Z])?\s*'   # callsign + sufixo opcional (1 letra maiúscula)

RE_NEW = re.compile(
    _TS + r': New client '   + _CALL + r'at (\S+) added with protocol (\S+) on module ([A-Z])'
)
RE_REM = re.compile(
    _TS + r': Client '       + _CALL + r'at \S+ removed'
)
RE_OPEN = re.compile(
    _TS + r': Opening stream on module ([A-Z]) for client ' + _CALL + r'with sid \d+'
)
RE_CLOSE = re.compile(
    _TS + r': Closing stream of module ([A-Z])'
)
RE_START = re.compile(r'Started xlxd\.service')


def _fmt_cs(cs: str, sf) -> str:
    """Formata callsign: ('PP5KX', 'A') → 'PP5KX-A' ; ('ECHO', None) → 'ECHO'."""
    cs = cs.strip()
    return f"{cs}-{sf}" if sf and sf.strip() else cs


def parse_xlx_log(logfile: str, tail: int = LOG_TAIL_LINES) -> dict:
    """
    Lê as últimas `tail` linhas do log e retorna:

      last_heard : lista de dicts, mais recente primeiro
                   [{callsign, module, protocol, hora, duracao_s}]

      clients    : dict {callsign: {module, protocol}}
                   apenas clientes de rádio (protocol != XLX)

      error      : string de erro ou None
    """
    try:
        with open(logfile, 'rb') as f:
            f.seek(0, 2)
            blk = min(f.tell(), tail * 130)
            f.seek(-blk, 2)
            raw = f.read().decode('utf-8', errors='replace')
    except OSError as e:
        logging.warning("Não foi possível ler %s: %s", logfile, e)
        return {'last_heard': [], 'clients': {}, 'error': str(e)}

    lines = raw.splitlines()[-tail:]

    # Considera apenas a sessão atual (a partir do último "Started xlxd")
    start_idx = 0
    for i, ln in enumerate(lines):
        if RE_START.search(ln):
            start_idx = i
    lines = lines[start_idx:]

    # Estado durante o parse
    clients      = {}   # cs_fmt → {module, protocol}
    open_streams = {}   # module → {callsign, protocol, hora, hora_s}
    last_heard   = []   # lista de TXes completos (Opening+Closing)

    for ln in lines:

        # ── Nova conexão de cliente ────────────────────────────────────────
        m = RE_NEW.search(ln)
        if m:
            ts, cs, sf, ip, proto, mod = m.groups()
            if proto.upper() == 'XLX':
                continue    # peer/interlink — não conta como cliente de rádio
            key = _fmt_cs(cs, sf)
            hora = ts.split(', ')[1]
            clients[key] = {'module': mod, 'protocol': proto, 'hora': hora}
            continue

        # ── Desconexão de cliente ──────────────────────────────────────────
        m = RE_REM.search(ln)
        if m:
            ts, cs, sf = m.groups()
            clients.pop(_fmt_cs(cs, sf), None)
            continue

        # ── Início de transmissão ──────────────────────────────────────────
        m = RE_OPEN.search(ln)
        if m:
            ts, mod, cs, sf = m.groups()
            hora = ts.split(', ')[1]
            key  = _fmt_cs(cs, sf)
            proto = clients.get(key, {}).get('protocol', '---')
            open_streams[mod] = {
                'callsign': key,
                'protocol': proto,
                'module'  : mod,
                'hora'    : hora,
                'hora_s'  : hms_para_seg(hora),
            }
            continue

        # ── Fim de transmissão ─────────────────────────────────────────────
        m = RE_CLOSE.search(ln)
        if m:
            ts, mod = m.groups()
            stream = open_streams.pop(mod, None)
            if stream:
                hora_fim = ts.split(', ')[1]
                dur = hms_para_seg(hora_fim) - stream['hora_s']
                last_heard.append({
                    'callsign' : stream['callsign'],
                    'module'   : mod,
                    'protocol' : stream['protocol'],
                    'hora'     : stream['hora'],     # hora de início da TX
                    'duracao_s': max(0, dur),
                })
            continue

    # Mais recentes primeiro, limitado a MAX_LASTHEARD
    return {
        'last_heard': list(reversed(last_heard))[:MAX_LASTHEARD],
        'clients'   : clients,
        'error'     : None,
    }


# =============================================================================
#  MONITOR PRINCIPAL
# =============================================================================

class XLXMonitor:

    def __init__(self, simulate: bool = False, invert: bool = False) -> None:
        self.simulate    = simulate
        self.invert      = invert
        self.hostname    = socket.gethostname()
        self._partial_ok  = False
        self._dual_buffer = False

        self.data    = {'last_heard': [], 'clients': {}, 'error': None, 'ip': '…'}
        self.running = False
        self._lock       = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        self.fonte_hdr = carregar_fonte(13)   # cabeçalho
        self.fonte_lh  = carregar_fonte(11)   # last heard
        self.fonte_sm  = carregar_fonte(10)   # status / rodapé

        if not simulate:
            self._inicializar_epd()

    # ── Hardware ──────────────────────────────────────────────────────────────

    @staticmethod
    def _encontrar_metodo(obj, *nomes):
        for nome in nomes:
            if hasattr(obj, nome) and callable(getattr(obj, nome)):
                return getattr(obj, nome), nome
        return None, None

    def _inicializar_epd(self) -> None:
        if os.path.exists(EPD_LIBDIR):
            sys.path.insert(0, EPD_LIBDIR)
        try:
            from waveshare_epd import epd2in13_V4 as epd_mod
            self._dual_buffer = False
            logging.info("Driver: epd2in13_V4 (HAT+ B/W)")
        except ImportError:
            try:
                from waveshare_epd import epd2in13b_V4 as epd_mod
                self._dual_buffer = True
                logging.warning("epd2in13_V4 não encontrado — usando epd2in13b_V4.")
            except ImportError as e:
                logging.error("Nenhum driver WaveShare encontrado: %s", e)
                sys.exit(1)

        self._epd_mod = epd_mod
        self.epd      = epd_mod.EPD()

        self._fn_partial,   _ = self._encontrar_metodo(
            self.epd, 'displayPartial', 'display_partial')
        self._fn_set_base,  _ = self._encontrar_metodo(
            self.epd, 'displayPartBaseImage', 'displayBase')
        self._fn_init_part, _ = self._encontrar_metodo(
            self.epd, 'init_fast', 'init_part', 'init_partial')

        self._partial_ok = self._fn_partial is not None
        logging.info("Refresh parcial: %s", "OK" if self._partial_ok else "indisponível")

    # ── Thread de leitura do log ──────────────────────────────────────────────

    def _refresh_data(self) -> None:
        logging.info("Thread log iniciada (intervalo: %ds)", LOG_INTERVAL)
        while not self._stop_event.is_set():
            resultado = parse_xlx_log(XLX_LOG)
            resultado['ip'] = get_ip_principal()
            with self._lock:
                self.data = resultado
            n_lh  = len(resultado['last_heard'])
            n_cli = len(resultado['clients'])
            if resultado['error']:
                logging.warning("Erro ao ler log: %s", resultado['error'])
            else:
                logging.debug("Log OK — %d last heard | %d clientes online", n_lh, n_cli)
            for _ in range(LOG_INTERVAL):
                if self._stop_event.is_set():
                    break
                time.sleep(1)
        logging.info("Thread log encerrada.")

    def start(self) -> None:
        self.running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._refresh_data,
                                        name='log-worker', daemon=True)
        self._thread.start()
        logging.info("XLXMonitor started.")

    def stop(self) -> None:
        self.running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logging.info("XLXMonitor stopped.")

    # ── Renderização ──────────────────────────────────────────────────────────

    def renderizar(self) -> Image.Image:
        with self._lock:
            lh  = list(self.data.get('last_heard', []))
            cli = dict(self.data.get('clients',    {}))
            ip  = self.data.get('ip',    'N/A')
            err = self.data.get('error', None)

        agora = datetime.now()
        dia   = DIAS[agora.weekday()]
        mes   = MESES[agora.month]

        bg = BLACK if self.invert else WHITE
        fg = WHITE if self.invert else BLACK

        img = Image.new('1', (W, H), bg)
        db  = ImageDraw.Draw(img)

        # ── CABEÇALHO ─────────────────────────────────────────────────────────
        db.text((3, 2), REFLECTOR_NAME, font=self.fonte_hdr, fill=fg)
        ts_str    = agora.strftime('%H:%M:%S')
        hdr_right = f"{dia} {agora.day:02d}/{agora.month:02d}  {ts_str}"
        larg_hdr  = largura_texto(db, hdr_right, self.fonte_hdr)
        db.text((W - larg_hdr - 2, 2), hdr_right, font=self.fonte_hdr, fill=fg)
        db.line([(0, Y_SEP1), (W - 1, Y_SEP1)], fill=fg)

        # ── LAST HEARD ────────────────────────────────────────────────────────
        if err and not lh:
            db.text((COL_CS, Y_LH0 + LH_STEP),
                    f"Erro: {err[:32]}", font=self.fonte_sm, fill=fg)
        elif not lh:
            # Sem atividade: centraliza mensagem na área
            msg = "Sem atividade recente"
            lm  = largura_texto(db, msg, self.fonte_sm)
            db.text(((W - lm) // 2, Y_LH0 + LH_STEP * 2),
                    msg, font=self.fonte_sm, fill=fg)
        else:
            for i, tx in enumerate(lh):
                y     = Y_LH0 + i * LH_STEP
                cs    = tx['callsign'][:10]
                mod   = tx['module']
                proto = tx['protocol'][:4]
                hora  = tx['hora']
                dur   = formatar_duracao(tx['duracao_s'])

                db.text((COL_CS,    y), cs,          font=self.fonte_lh, fill=fg)
                db.text((COL_MOD,   y), f"[{mod}]",  font=self.fonte_lh, fill=fg)
                db.text((COL_PROTO, y), proto,        font=self.fonte_lh, fill=fg)
                db.text((COL_HORA,  y), hora,         font=self.fonte_lh, fill=fg)
                # Duração alinhada à direita
                larg_d = largura_texto(db, dur, self.fonte_lh)
                db.text((W - larg_d - 2, y), dur,    font=self.fonte_lh, fill=fg)

        db.line([(0, Y_SEP2), (W - 1, Y_SEP2)], fill=fg)

        # ── CLIENTES ONLINE ───────────────────────────────────────────────────
        n   = len(cli)
        if n == 0:
            online_str = "Sem clientes online"
        else:
            # Lista de callsigns separados por espaço
            cs_list    = '  '.join(sorted(cli.keys())[:5])
            online_str = f"{cs_list}"
        db.text((COL_CS, Y_ONLINE), online_str[:36], font=self.fonte_sm, fill=fg)
        # Contagem alinhada à direita
        cnt_str  = f"{n} online"
        larg_cnt = largura_texto(db, cnt_str, self.fonte_sm)
        db.text((W - larg_cnt - 2, Y_ONLINE), cnt_str, font=self.fonte_sm, fill=fg)

        db.line([(0, Y_SEP3), (W - 1, Y_SEP3)], fill=fg)

        # ── RODAPÉ ────────────────────────────────────────────────────────────
        db.text((COL_CS, Y_FOOT), ip, font=self.fonte_sm, fill=fg)
        larg_cs = largura_texto(db, CALLSIGN, self.fonte_sm)
        db.text((W - larg_cs - 2, Y_FOOT), CALLSIGN, font=self.fonte_sm, fill=fg)

        return img

    # ── Controle do display ───────────────────────────────────────────────────

    def _buf(self, img: Image.Image):
        return self.epd.getbuffer(img)

    def _do_full_refresh(self) -> None:
        img = self.renderizar()
        buf = self._buf(img)
        if self._dual_buffer:
            self.epd.display(buf, self._buf(Image.new('1', (W, H), WHITE)))
        else:
            self.epd.display(buf)
        if self._fn_set_base:
            self._fn_set_base(buf)
        logging.info("Full refresh concluído.")

    def _do_partial_refresh(self) -> None:
        self._fn_partial(self._buf(self.renderizar()))

    def _init_part(self) -> None:
        if self._fn_init_part:
            self._fn_init_part()

    # ── Tela de desligamento ──────────────────────────────────────────────────

    def _desenhar_tela_desligamento(self) -> Image.Image:
        img = Image.new('1', (W, H), BLACK)
        db  = ImageDraw.Draw(img)
        fonte_cs = carregar_fonte(48)

        # Antena
        ax, ay = 30, 95
        db.line([(ax, ay), (ax, ay - 38)], fill=WHITE, width=2)
        db.line([(ax - 14, ay - 20), (ax + 14, ay - 20)], fill=WHITE, width=2)
        db.line([(ax - 9,  ay - 30), (ax + 9,  ay - 30)], fill=WHITE, width=2)

        # Ondas irradiando
        ox, oy = ax, ay - 38
        for r in [12, 22, 34]:
            db.arc([ox - r, oy - r, ox + r, oy + r], start=210, end=310, fill=WHITE)
            db.arc([ox - r, oy - r, ox + r, oy + r], start=230, end=330, fill=WHITE)
        for r in [14, 26, 40]:
            db.arc([ox - r, oy - r, ox + r, oy + r], start=300, end=60,  fill=WHITE)

        # Indicativo centralizado
        larg = largura_texto(db, CALLSIGN, fonte_cs)
        db.text((70 + (W - 70 - larg) // 2, (H - 48) // 2),
                CALLSIGN, font=fonte_cs, fill=WHITE)

        db.line([(63, 10), (63, H - 10)], fill=WHITE, width=1)
        db.rectangle([1, 1, W - 2, H - 2], outline=WHITE)
        return img

    # ── Loop principal ────────────────────────────────────────────────────────

    def executar(self) -> None:
        logging.info("XLX Monitor | log: %ds | limpeza: %ds | inversão: %ds",
                     LOG_INTERVAL, CLEAN_INTERVAL, INVERT_INTERVAL)
        self.start()

        if self.simulate:
            self._loop_simulate()
            self.stop()
            return

        logging.info("Inicializando e-Paper...")
        self.epd.init()
        self.epd.Clear()

        t_clean  = time.monotonic()
        t_invert = time.monotonic()

        self._do_full_refresh()
        if self._partial_ok:
            self._init_part()

        try:
            while True:
                sleep_ate_proximo_segundo()
                agora = time.monotonic()

                if agora - t_invert >= INVERT_INTERVAL:
                    self.invert = not self.invert
                    logging.info("Inversão de cor → fundo %s",
                                 "preto" if self.invert else "branco")
                    self.epd.init()
                    self.epd.Clear()
                    self._do_full_refresh()
                    t_invert = t_clean = agora
                    if self._partial_ok:
                        self._init_part()
                    continue

                if agora - t_clean >= CLEAN_INTERVAL:
                    logging.info("Limpeza anti-ghosting...")
                    self.epd.init()
                    self.epd.Clear()
                    self._do_full_refresh()
                    t_clean = agora
                    if self._partial_ok:
                        self._init_part()
                    continue

                if self._partial_ok:
                    self._do_partial_refresh()
                else:
                    self.epd.init()
                    self._do_full_refresh()

        except KeyboardInterrupt:
            logging.info("Interrompido pelo usuário.")
        finally:
            self.stop()
            logging.info("Exibindo tela de desligamento...")
            self.epd.init()
            self.epd.Clear()
            img_bye = self._desenhar_tela_desligamento()
            buf_bye = self._buf(img_bye)
            if self._dual_buffer:
                self.epd.display(buf_bye, self._buf(Image.new('1', (W, H), BLACK)))
            else:
                self.epd.display(buf_bye)
            time.sleep(1)
            self.epd.sleep()
            self._epd_mod.epdconfig.module_exit(cleanup=True)

    def _loop_simulate(self) -> None:
        logging.info("Modo simulação — Ctrl+C para encerrar")
        try:
            while True:
                sleep_ate_proximo_segundo()
                img = self.renderizar()
                img.convert('RGB').save('/tmp/epd_preview.png')
                logging.info("→ /tmp/epd_preview.png  [%s]",
                             datetime.now().strftime('%H:%M:%S'))
        except KeyboardInterrupt:
            logging.info("Simulação encerrada.")

    def executar_uma_vez(self) -> None:
        resultado       = parse_xlx_log(XLX_LOG)
        resultado['ip'] = get_ip_principal()
        with self._lock:
            self.data = resultado
        if self.simulate:
            self.renderizar().convert('RGB').save('/tmp/epd_preview.png')
            logging.info("Simulação salva → /tmp/epd_preview.png")
            return
        self.epd.init()
        self._do_full_refresh()
        self.epd.sleep()
        self._epd_mod.epdconfig.module_exit(cleanup=True)


# =============================================================================
#  ENTRY POINT
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Monitor XLX — e-Paper WaveShare 2.13" HAT+ (epd2in13_V4)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  python3 e-paper_monitor.py               # loop contínuo
  python3 e-paper_monitor.py --black       # fundo preto
  python3 e-paper_monitor.py --simulate    # preview PNG sem hardware
  python3 e-paper_monitor.py --once        # atualiza uma vez e sai
        """,
    )
    parser.add_argument('--simulate', action='store_true',
                        help='Renderiza em /tmp/epd_preview.png (sem hardware)')
    parser.add_argument('--once',     action='store_true',
                        help='Atualiza uma única vez e encerra')
    parser.add_argument('--black',    action='store_true',
                        help='Inverte as cores: fundo preto, texto branco')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S',
    )

    if args.black:
        logging.info("Modo invertido: fundo preto / texto branco")

    monitor = XLXMonitor(simulate=args.simulate, invert=args.black)
    if args.once:
        monitor.executar_uma_vez()
    else:
        monitor.executar()


if __name__ == '__main__':
    main()
