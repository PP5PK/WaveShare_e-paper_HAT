#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
station_monitor.py — Monitor de Sistema para e-Paper 2.13" B/W (HAT+)
======================================================================
Indicativo · Data · Relógio HH:MM:SS (atualiza 1×/s) · CPU · Temp · RAM · Disco

Display  : WaveShare 2.13" e-Paper HAT+ (epd2in13_V4)   250 × 122 px

Nota sobre o driver
───────────────────
  epd2in13_V4   → HAT+ preto/branco  → display(1 buffer), displayPartial() ✓
  epd2in13b_V4  → HAT tricolor B/W/R → display(2 buffers), sem partial refresh ✗
  O código tenta epd2in13_V4 primeiro; se não encontrar, avisa e tenta o b_V4.
Autor    : PP5KX

Arquitetura de threads
──────────────────────
  Thread stats  — coleta CPU/RAM/Temp/Disco a cada STATS_INTERVAL (5 s) em background
                  (cpu_percent(interval=1) é bloqueante — thread separada é essencial)
  Thread main   — acorda no próximo segundo exato do relógio de parede,
                  renderiza e chama displayPartial (~0,3 s)
                  Sem drift: o relógio avança em :00, :01, :02 ...

Ciclos de refresh
─────────────────
  Parcial  (1 s)              — displayPartial() → relógio + stats cacheados (~0,3 s)
  Limpeza  (CLEAN_INTERVAL s) — epd.Clear() + full refresh a cada 15 min (anti-ghosting)
                                Único momento de full refresh — todo o resto é parcial

Uso
───
  python3 station_monitor.py              # loop contínuo
  python3 station_monitor.py --simulate   # salva /tmp/epd_preview.png a cada 1 s
  python3 station_monitor.py --once       # full refresh único e encerra

Serviço systemd (opcional)
──────────────────────────
  [Unit]
  Description=Station Monitor e-Paper
  After=network.target

  [Service]
  ExecStart=/usr/bin/python3 /home/pi/e-Paper/RaspberryPi_JetsonNano/python/examples/station_monitor.py
  Restart=always
  RestartSec=5
  User=pi

  [Install]
  WantedBy=multi-user.target
"""

import sys
import os
import time
import logging
import socket
import argparse
import threading
import psutil
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

# =============================================================================
#  CONFIGURAÇÃO
# =============================================================================

CALLSIGN       = "PP5KX"   # Indicativo exibido no cabeçalho
DISK_PATH      = "/"       # Ponto de montagem monitorado

STATS_INTERVAL  = 5         # Segundos entre coletas de CPU/RAM/Temp/Disco (via thread)
CLEAN_INTERVAL  = 600       # Segundos entre limpezas anti-ghosting (epd.Clear + full refresh)
INVERT_INTERVAL = 1800      # Segundos entre inversões automáticas de cor (anti-ghosting permanente)

# Caminhos da biblioteca WaveShare (estrutura padrão do repositório oficial)
_BASE      = os.path.dirname(os.path.abspath(__file__))
EPD_LIBDIR = os.path.join(_BASE, '..', 'lib')
EPD_PICDIR = os.path.join(_BASE, '..', 'pic')

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
#  LAYOUT  250 × 122 px landscape
# =============================================================================

W, H         = 250, 122
WHITE, BLACK = 255, 0

Y_SEP1    = 19     # separador após cabeçalho (fonte_md 15pt → ~17px)
Y_CLOCK   = 23     # início do relógio  (font 42 → ~45 px de altura)
Y_SEP2    = 71     # separador após relógio
Y_ROW1    = 74     # CPU + Temperatura
Y_ROW2    = 89     # RAM + Disco
Y_FOOT    = 107    # rodapé: hostname

BAR_LX    = 33     # x início da barra
BAR_W     = 64     # largura da barra
BAR_H     = 8      # altura da barra
BAR_VAL_X = 100    # x do valor "%"
MID_SEP_X = 136    # x separador vertical central
RIGHT_LX  = 140    # x coluna direita (Temp / Disco)


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
    logging.warning("Fonte TrueType não encontrada — usando bitmap padrão.")
    return ImageFont.load_default()


def largura_texto(draw: ImageDraw.Draw, texto: str, fonte) -> int:
    bbox = draw.textbbox((0, 0), texto, font=fonte)
    return bbox[2] - bbox[0]


def desenhar_barra(db: ImageDraw.Draw, x: int, y: int, w: int, h: int,
                   pct: float, fg: int = BLACK) -> None:
    pct = max(0.0, min(100.0, pct))
    preenchido = int(w * pct / 100)
    db.rectangle([x, y, x + w, y + h], outline=fg)
    if preenchido > 1:
        db.rectangle([x + 1, y + 1, x + preenchido - 1, y + h - 1], fill=fg)


def get_ip_principal() -> str:
    """
    Retorna o IP da interface usada para alcançar a internet
    (equivale à 'ethernet principal' em termos práticos).
    Usa um socket UDP sem enviar dados — apenas consulta a rota.
    Fallback: varre eth0/wlan0 com psutil se o socket falhar.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
    except OSError:
        pass
    # Fallback: interfaces em ordem de preferência
    try:
        addrs = psutil.net_if_addrs()
        for iface in ('eth0', 'eth1', 'en0', 'enp0s3', 'wlan0', 'wlan1'):
            if iface in addrs:
                for addr in addrs[iface]:
                    if addr.family == socket.AF_INET:
                        return addr.address
    except Exception:
        pass
    return 'N/A'



def cpu_temperatura() -> float:
    """Lê temperatura da CPU em °C; retorna -1.0 se indisponível."""
    try:
        sensores = psutil.sensors_temperatures()
        if sensores:
            for chave in ('cpu_thermal', 'cpu-thermal', 'coretemp', 'k10temp', 'acpitz'):
                if chave in sensores and sensores[chave]:
                    return sensores[chave][0].current
            for valores in sensores.values():
                if valores:
                    return valores[0].current
    except AttributeError:
        pass
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            return int(f.read().strip()) / 1000.0
    except OSError:
        return -1.0


def sleep_ate_proximo_segundo() -> None:
    """
    Dorme até o próximo segundo inteiro do relógio de parede.
    Elimina drift acumulado: independente de quanto tempo levou
    o displayPartial (0,3 s) ou a renderização, o próximo ciclo
    sempre começa exatamente no :00, :01, :02 ...
    """
    frac = time.time() % 1.0
    time.sleep(1.0 - frac)


# =============================================================================
#  MONITOR PRINCIPAL
# =============================================================================

class StationMonitor:
    """
    Combina a arquitetura de threading do rascunho original
    (start/stop/refresh_data/fetch_data) com loop de display sincronizado
    ao relógio de parede e refresh parcial a cada segundo.
    """

    def __init__(self, simulate: bool = False, invert: bool = False) -> None:
        self.simulate    = simulate
        self.invert      = invert          # True → fundo preto, texto branco
        self.hostname    = socket.gethostname()
        self._partial_ok  = False
        self._dual_buffer = False

        # ── Estado compartilhado (padrão do rascunho: self.data / self.running)
        self.data    = self._stats_vazios()
        self.running = False
        self._lock         = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event   = threading.Event()

        # ── Fontes (data 15pt, relógio 42pt → "HH:MM:SS" cabe em 250 px)
        self.fonte_sm    = carregar_fonte(11)
        self.fonte_host  = carregar_fonte(11)   # hostname menor para não emender com a data
        self.fonte_md    = carregar_fonte(15)
        self.fonte_clock = carregar_fonte(42)

        if not simulate:
            self._inicializar_epd()

    # ── Hardware ──────────────────────────────────────────────────────────────

    @staticmethod
    def _encontrar_metodo(obj, *nomes):
        """Retorna (callable, nome) do primeiro método encontrado, ou (None, None)."""
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
                logging.warning("epd2in13_V4 não encontrado — usando epd2in13b_V4 (tricolor).")
            except ImportError as e:
                logging.error("Nenhum driver WaveShare encontrado em %s: %s", EPD_LIBDIR, e)
                sys.exit(1)

        self._epd_mod = epd_mod
        self.epd      = epd_mod.EPD()

        # Método de partial refresh (obrigatório para modo parcial)
        self._fn_partial, nome_p = self._encontrar_metodo(
            self.epd, 'displayPartial', 'display_partial', 'DisplayPartial',
        )

        # Imagem base do partial — deve ser enviada após cada full refresh
        # para que o display saiba quais pixels "não mudaram"
        self._fn_set_base, nome_b = self._encontrar_metodo(
            self.epd, 'displayPartBaseImage', 'displayBase', 'display_base_image',
        )

        # Re-init para modo parcial (opcional — alguns drivers não precisam)
        self._fn_init_part, nome_i = self._encontrar_metodo(
            self.epd, 'init_fast', 'init_part', 'init_Part', 'init_partial',
        )

        # Partial OK se o método de atualização parcial existir
        self._partial_ok = self._fn_partial is not None

        if self._partial_ok:
            logging.info(
                "Refresh parcial ativo | partial: %s | base: %s | re-init: %s",
                nome_p,
                nome_b or "não disponível",
                nome_i or "não necessário",
            )
        else:
            logging.warning("displayPartial não encontrado — usando somente full refresh.")
            metodos = [m for m in dir(self.epd) if not m.startswith('_')]
            logging.warning("Métodos disponíveis: %s", ', '.join(metodos))

    # ── Thread de stats (padrão do rascunho original) ─────────────────────────

    @staticmethod
    def _stats_vazios() -> dict:
        return {'cpu_pct': 0.0, 'ram_pct': 0.0, 'disco_pct': 0.0, 'temp': -1.0, 'ip': '…'}

    def fetch_data(self) -> dict:
        """
        Coleta métricas reais do sistema.
        cpu_percent(interval=1) bloqueia ~1 s — thread separada é essencial.
        """
        cpu_pct = psutil.cpu_percent(interval=1)
        mem     = psutil.virtual_memory()
        try:
            disco_pct = psutil.disk_usage(DISK_PATH).percent
        except OSError:
            disco_pct = -1.0
        return {
            'cpu_pct'  : cpu_pct,
            'ram_pct'  : mem.percent,
            'disco_pct': disco_pct,
            'temp'     : cpu_temperatura(),
            'ip'       : get_ip_principal(),
        }

    def refresh_data(self) -> None:
        """
        Loop da thread de stats.
        Mesma estrutura do refresh_data() do rascunho original:
        chama fetch_data() em loop, atualiza self.data, dorme STATS_INTERVAL.
        Usa _stop_event para encerramento limpo sem join() longo.
        """
        logging.info("Thread stats iniciada (intervalo: %ds)", STATS_INTERVAL)
        while not self._stop_event.is_set():
            novo = self.fetch_data()
            with self._lock:
                self.data = novo
            logging.debug(
                "Stats — CPU %.0f%% | RAM %.0f%% | Temp %s°C | Disk %.0f%%",
                novo['cpu_pct'], novo['ram_pct'],
                f"{novo['temp']:.0f}" if novo['temp'] >= 0 else "N/A",
                novo['disco_pct'] if novo['disco_pct'] >= 0 else 0,
            )
            # Dorme em fatias de 1 s para responder rápido ao stop_event
            for _ in range(STATS_INTERVAL):
                if self._stop_event.is_set():
                    break
                time.sleep(1)
        logging.info("Thread stats encerrada.")

    def start(self) -> None:
        """Inicia a thread de stats (padrão do rascunho original)."""
        self.running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self.refresh_data,
            name='stats-worker',
            daemon=True,
        )
        self._thread.start()
        logging.info("StationMonitor started.")   # mensagem original do rascunho

    def stop(self) -> None:
        """Para a thread de stats (padrão do rascunho original)."""
        self.running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logging.info("StationMonitor stopped.")   # mensagem original do rascunho

    # ── Renderização ──────────────────────────────────────────────────────────

    def renderizar(self) -> Image.Image:
        """
        Gera o buffer 1-bit da tela.
        Stats: lidos de self.data com Lock (escritos pela thread de stats).
        Relógio: datetime.now() — sempre reflete o segundo exato atual.
        Cores: self.invert=False → fundo branco / texto preto (padrão)
               self.invert=True  → fundo preto  / texto branco (--black)
        """
        with self._lock:
            stats = dict(self.data)

        agora = datetime.now()
        dia   = DIAS[agora.weekday()]
        mes   = MESES[agora.month]

        bg = BLACK if self.invert else WHITE   # cor do fundo
        fg = WHITE if self.invert else BLACK   # cor do texto / linhas

        img = Image.new('1', (W, H), bg)
        db  = ImageDraw.Draw(img)

        # ── CABEÇALHO: hostname (esq) · data (dir) ───────────────────────────
        db.text((3, 4), self.hostname[:18], font=self.fonte_host, fill=fg)
        data_str  = f"{dia}, {agora.day:02d} {mes} {agora.year}"
        larg_data = largura_texto(db, data_str, self.fonte_md)
        db.text((W - larg_data - 3, 2), data_str, font=self.fonte_md, fill=fg)
        db.line([(0, Y_SEP1), (W - 1, Y_SEP1)], fill=fg)

        # ── RELÓGIO HH:MM:SS ──────────────────────────────────────────────────
        hora_str  = agora.strftime('%H:%M:%S')
        larg_hora = largura_texto(db, hora_str, self.fonte_clock)
        db.text(((W - larg_hora) // 2, Y_CLOCK), hora_str,
                font=self.fonte_clock, fill=fg)
        db.line([(0, Y_SEP2), (W - 1, Y_SEP2)], fill=fg)

        # ── SEPARADOR VERTICAL ────────────────────────────────────────────────
        db.line([(MID_SEP_X, Y_SEP2 + 1), (MID_SEP_X, H - 1)], fill=fg)

        # ── ROW 1: CPU + TEMPERATURA ──────────────────────────────────────────
        y1 = Y_ROW1
        db.text((2, y1), 'CPU', font=self.fonte_sm, fill=fg)
        desenhar_barra(db, BAR_LX, y1 + 3, BAR_W, BAR_H, stats['cpu_pct'], fg)
        db.text((BAR_VAL_X, y1), f"{stats['cpu_pct']:4.0f}%",
                font=self.fonte_sm, fill=fg)
        temp_str = (f"Temp {stats['temp']:.0f}\u00b0C"
                    if stats['temp'] >= 0 else 'Temp N/A')
        db.text((RIGHT_LX, y1), temp_str, font=self.fonte_sm, fill=fg)

        # ── ROW 2: RAM + DISCO ────────────────────────────────────────────────
        y2 = Y_ROW2
        db.text((2, y2), 'RAM', font=self.fonte_sm, fill=fg)
        desenhar_barra(db, BAR_LX, y2 + 3, BAR_W, BAR_H, stats['ram_pct'], fg)
        db.text((BAR_VAL_X, y2), f"{stats['ram_pct']:4.0f}%",
                font=self.fonte_sm, fill=fg)
        disco_str = (f"Disk {stats['disco_pct']:.0f}%"
                     if stats['disco_pct'] >= 0 else 'Disk N/A')
        db.text((RIGHT_LX, y2), disco_str, font=self.fonte_sm, fill=fg)

        # ── RODAPÉ: IP (esq) · indicativo (dir) ─────────────────────────────
        db.text((2, Y_FOOT), stats['ip'], font=self.fonte_sm, fill=fg)
        larg_cs = largura_texto(db, CALLSIGN, self.fonte_sm)
        db.text((W - larg_cs - 2, Y_FOOT), CALLSIGN, font=self.fonte_sm, fill=fg)

        return img

    # ── Controle do display ───────────────────────────────────────────────────

    def _buf(self, img: Image.Image):
        return self.epd.getbuffer(img)

    def _do_full_refresh(self) -> None:
        """Full refresh + define imagem base para o próximo ciclo de partial."""
        img = self.renderizar()
        buf = self._buf(img)
        if self._dual_buffer:
            self.epd.display(buf, self._buf(Image.new('1', (W, H), WHITE)))
        else:
            self.epd.display(buf)
        # Registra a imagem atual como "fundo" para o partial refresh.
        # Sem isso o display não sabe quais pixels mudaram e faz full refresh interno.
        if self._fn_set_base:
            self._fn_set_base(buf)
        logging.info("Full refresh concluído.")

    def _do_partial_refresh(self) -> None:
        img = self.renderizar()
        self._fn_partial(self._buf(img))

    def _init_part(self) -> None:
        """Re-inicializa o display para modo parcial.
        No epd2in13_V4 usa init_fast(); em outros drivers pode ser init_part().
        Se nenhum método existir, displayPartial funciona diretamente após display().
        """
        if self._fn_init_part:
            self._fn_init_part()

    def _desenhar_tela_desligamento(self) -> Image.Image:
        """
        Tela exibida ao encerrar o programa.
        Fundo preto, indicativo centralizado em destaque, ondas de rádio
        irradiando de uma antena e '73' — saudação clássica do radioamadorismo.
        Tudo desenhado via primitivas PIL, sem imagens externas.
        """
        img = Image.new('1', (W, H), BLACK)
        db  = ImageDraw.Draw(img)

        fonte_cs  = carregar_fonte(48)   # indicativo em destaque máximo

        # ── Antena: mastro vertical + dois elementos horizontais ──────────────
        ax, ay = 30, 95          # base da antena
        db.line([(ax, ay), (ax, ay - 38)], fill=WHITE, width=2)                  # mastro
        db.line([(ax - 14, ay - 20), (ax + 14, ay - 20)], fill=WHITE, width=2)  # elemento 1
        db.line([(ax - 9,  ay - 30), (ax + 9,  ay - 30)], fill=WHITE, width=2)  # elemento 2

        # ── Ondas irradiando da ponta da antena (arcos simétricos) ───────────
        ox, oy = ax, ay - 38
        for r in [12, 22, 34]:
            db.arc([ox - r, oy - r, ox + r, oy + r], start=210, end=310, fill=WHITE)
            db.arc([ox - r, oy - r, ox + r, oy + r], start=230, end=330, fill=WHITE)

        # ── Ondas direcionais para a direita (propagação) ─────────────────────
        px, py = ax + 2, ay - 38
        for r in [14, 26, 40]:
            db.arc([px - r, py - r, px + r, py + r], start=300, end=60, fill=WHITE)

        # ── Indicativo centralizado verticalmente no campo direito ────────────
        larg_cs = largura_texto(db, CALLSIGN, fonte_cs)
        cx = 70 + (W - 70 - larg_cs) // 2
        cy = (H - 48) // 2
        db.text((cx, cy), CALLSIGN, font=fonte_cs, fill=WHITE)

        # ── Linha separadora vertical entre antena e texto ────────────────────
        db.line([(63, 10), (63, H - 10)], fill=WHITE, width=1)

        # ── Borda fina ao redor da tela ───────────────────────────────────────
        db.rectangle([1, 1, W - 2, H - 2], outline=WHITE)

        return img

    # ── Loop principal ────────────────────────────────────────────────────────

    def executar(self) -> None:
        """
        Inicia a thread de stats e entra no loop de display.
        Sincroniza com o relógio de parede via sleep_ate_proximo_segundo().
        """
        logging.info(
            "Display loop | stats: %ds | limpeza: %ds | inversão: %ds",
            STATS_INTERVAL, CLEAN_INTERVAL, INVERT_INTERVAL,
        )

        self.start()   # thread de stats

        if self.simulate:
            self._loop_simulate()
            self.stop()
            return

        # ── Inicialização do hardware ──────────────────────────────────────
        logging.info("Inicializando e-Paper...")
        self.epd.init()
        self.epd.Clear()

        t_clean  = time.monotonic()
        t_invert = time.monotonic()

        self._do_full_refresh()   # primeiro frame
        if self._partial_ok:
            self._init_part()

        try:
            while True:
                sleep_ate_proximo_segundo()
                agora = time.monotonic()

                # ── Inversão automática de cor a cada INVERT_INTERVAL ─────
                # Alterna fundo branco ↔ preto para evitar marcação permanente
                if agora - t_invert >= INVERT_INTERVAL:
                    self.invert = not self.invert
                    logging.info("Inversão de cor → fundo %s",
                                 "preto" if self.invert else "branco")
                    self.epd.init()
                    self.epd.Clear()
                    self._do_full_refresh()
                    t_invert = agora
                    t_clean  = agora   # reset clean também — já limpamos
                    if self._partial_ok:
                        self._init_part()
                    continue

                # ── Limpeza anti-ghosting a cada CLEAN_INTERVAL ───────────
                if agora - t_clean >= CLEAN_INTERVAL:
                    logging.info("Limpeza anti-ghosting (full refresh)...")
                    self.epd.init()
                    self.epd.Clear()
                    self._do_full_refresh()
                    t_clean = agora
                    if self._partial_ok:
                        self._init_part()
                    continue

                # ── Refresh parcial: todo o resto (relógio + stats atuais) ─
                if self._partial_ok:
                    self._do_partial_refresh()
                else:
                    self.epd.init()
                    self._do_full_refresh()

        except KeyboardInterrupt:
            logging.info("Interrompido pelo usuário (Ctrl+C).")
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
        """Simulação: salva /tmp/epd_preview.png a cada segundo (sem hardware)."""
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
        """Full refresh único, sem thread de stats (uso com cron/timer)."""
        novo = self.fetch_data()
        with self._lock:
            self.data = novo

        if self.simulate:
            img = self.renderizar()
            img.convert('RGB').save('/tmp/epd_preview.png')
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
        description='Monitor de sistema — e-Paper WaveShare 2.13" HAT+ (epd2in13_V4)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  python3 station_monitor.py               # fundo branco (padrão)
  python3 station_monitor.py --black       # fundo preto, texto branco
  python3 station_monitor.py --simulate    # preview PNG em /tmp sem hardware
  python3 station_monitor.py --black --simulate
  python3 station_monitor.py --once        # full refresh único e sai
        """,
    )
    parser.add_argument('--simulate', action='store_true',
                        help='Renderiza em /tmp/epd_preview.png (sem hardware)')
    parser.add_argument('--once', action='store_true',
                        help='Atualiza uma única vez e encerra')
    parser.add_argument('--black', action='store_true',
                        help='Inverte as cores: fundo preto, texto branco')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S',
    )

    if args.black:
        logging.info("Modo invertido: fundo preto / texto branco")

    monitor = StationMonitor(simulate=args.simulate, invert=args.black)
    if args.once:
        monitor.executar_uma_vez()
    else:
        monitor.executar()


if __name__ == '__main__':
    main()
