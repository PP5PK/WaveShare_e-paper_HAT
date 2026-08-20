#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xlx_monitor.py — Monitor XLX para e-Paper 2.13" B/W (HAT+)
===============================================================
Exibe em tempo real informações do refletor XLX:
  · Últimas transmissões ouvidas (indicativo real, gateway, módulo, horário)
  · Clientes/gateways conectados agora, com protocolo
  · Relógio (HH:MM, sem segundos) atualizado 1x por minuto via partial refresh

Display  : WaveShare 2.13" e-Paper HAT+ (epd2in13_V4)  250 × 122 px
Fonte    : /var/log/xlxd.xml (único — o log de texto do xlxd NÃO é lido)
Autor    : PP5PK

Arquitetura de threads
──────────────────────
  Thread XML    — relê o XML a cada REFRESH_INTERVAL segundos
  Thread main   — acorda no início exato de cada minuto, renderiza via displayPartial

Ciclos de refresh
─────────────────
  Parcial  (1x/min)           — displayPartial() · relógio + last heard (~0,3 s)
  Limpeza  (CLEAN_INTERVAL s) — epd.Clear() + full refresh (anti-ghosting)
  Inversão (INVERT_INTERVAL s)— alterna fundo B/W para evitar marcação permanente

Uso
───
  python3 xlx_monitor.py              # loop contínuo
  python3 xlx_monitor.py --black      # fundo preto, texto branco
  python3 xlx_monitor.py --simulate   # salva /tmp/epd_preview.png a cada minuto
  python3 xlx_monitor.py --once       # full refresh único e encerra
  python3 xlx_monitor.py --off        # deixa a tela em branco e desliga
"""

import sys
import os
import re
import time
import logging
import argparse
import threading
import subprocess
import signal
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

# =============================================================================
#  CONFIGURAÇÃO
# =============================================================================

REFLECTOR_FALLBACK = "XLX Reflector"   # Só usado se o XML falhar/não tiver o nome ainda
XLX_XML         = "/var/log/xlxd.xml"  # Caminho do XML de status do xlxd (única fonte de dados)
MAX_LASTHEARD   = 12                   # Entradas de last heard retidas (exibição é dinâmica)

REFRESH_INTERVAL = 5                  # Segundos entre releituras do XML
CLEAN_INTERVAL   = 600                # Segundos entre limpezas anti-ghosting
INVERT_INTERVAL  = 1800               # Segundos entre inversões automáticas de cor

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

# =============================================================================
#  LAYOUT  250 × 122 px
# =============================================================================

W, H         = 250, 122
WHITE, BLACK = 255, 0

Y_SEP1    = 13    # separador após cabeçalho
Y_LH0     = 15    # primeira linha last heard (gap após separador: 3px -> 1px)
LH_STEP   = 8     # espaçamento entre linhas last heard
# Y_SEP2 / Y_CLI_TOP não são mais fixos — calculados em renderizar() conforme
# o espaço que a lista de clientes realmente precisa (ver GAP_LH_SEP/GAP_SEP_CLI)

# Colunas da tabela last heard (x em pixels)
COL_CS  = 2     # indicativo real do operador (sem sufixo, ex.: 'PP5PK')
COL_GW  = 48    # gateway+sufixo; se via peer linkado, formato 'GATEWAY via PEER'
MOD_HORA_GAP = 8   # espaço entre o módulo e o horário
# Módulo e horário são alinhados dinamicamente à direita (ver renderizar()),
# o que dá o máximo de espaço possível pro gateway em vez de uma coluna fixa.


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


def truncar_para_largura(draw: ImageDraw.Draw, texto: str, fonte,
                          largura_max: int) -> str:
    """Corta `texto` (do final) até caber em `largura_max` pixels."""
    if largura_texto(draw, texto, fonte) <= largura_max:
        return texto
    while texto and largura_texto(draw, texto, fonte) > largura_max:
        texto = texto[:-1]
    return texto


def detectar_gpiochip_rp1(label: str = "pinctrl-rp1") -> int:
    """
    Localiza dinamicamente o número do /dev/gpiochipN correspondente ao
    controlador de pinos do RP1 (Pi 5).

    Necessário porque o gpiozero assume chip=0 por padrão, mas a ordem de
    enumeração dos gpiochips varia conforme o kernel/firmware e outros
    controladores presentes no sistema (ex.: gpio-brcmstb) — no Pi 5 o
    RP1 pode acabar em qualquer número, não necessariamente 0 ou 4.

    Kernels atuais não expõem mais os labels via sysfs legado
    (/sys/bus/gpio/devices/*/label) — essa informação só está disponível
    pela interface de caractere via ioctl. Usamos o `gpiodetect`
    (libgpiod-tools) para consultar isso, com o sysfs como fallback para
    kernels mais antigos. Se nada for encontrado, retorna 0.
    """
    # 1) Kernels atuais: ioctl via gpiodetect (libgpiod-tools)
    try:
        saida = subprocess.run(['gpiodetect'], capture_output=True,
                                text=True, timeout=5)
        if saida.returncode == 0:
            for linha in saida.stdout.splitlines():
                m = re.match(r'(gpiochip\d+)\s+\[(.+?)\]', linha)
                if m and m.group(2) == label:
                    return int(m.group(1).replace('gpiochip', ''))
    except (FileNotFoundError, subprocess.SubprocessError):
        pass

    # 2) Fallback: sysfs legado (kernels mais antigos)
    base = "/sys/bus/gpio/devices"
    try:
        for entry in os.listdir(base):
            label_path = os.path.join(base, entry, "label")
            try:
                with open(label_path) as f:
                    if f.read().strip() == label:
                        return int(entry.replace("gpiochip", ""))
            except OSError:
                continue
    except OSError:
        pass

    return 0


def sleep_ate_proximo_minuto() -> None:
    """Dorme até o início exato do próximo minuto (segundo 0)."""
    agora    = time.time()
    proximo  = (agora // 60 + 1) * 60
    time.sleep(max(0.0, proximo - agora))


def hms_para_seg(t: str) -> int:
    """'HH:MM:SS' → total de segundos desde meia-noite."""
    try:
        h, m, s = t.split(':')
        return int(h) * 3600 + int(m) * 60 + int(s)
    except Exception:
        return 0


# =============================================================================
#  PARSER DO XML DE STATUS DO XLXD
# =============================================================================
#
# O xlxd escreve periodicamente um arquivo "XML" com o status corrente
# (não é XML estritamente válido: tags com espaço no nome, múltiplas
# raízes — por isso o parse é feito por regex, e não por ElementTree).
# É a ÚNICA fonte de dados da tela — o log de texto do xlxd não é mais lido.
#
# Seções relevantes:
#
#   <XLXBRA  linked peers>          → nome do refletor local vem daqui
#     <PEER>...</PEER>
#
#   <XLXBRA  linked nodes>          → clientes/gateways conectados agora
#     <NODE>
#         <Callsign>PP5PK   B</Callsign>     indicativo + sufixo do nó
#         <LinkedModule>D</LinkedModule>     módulo em que está linkado
#         <Protocol>DCS</Protocol>           protocolo da conexão
#         <LastHeardTime>...</LastHeardTime>
#     </NODE>
#     (um mesmo indicativo pode aparecer mais de uma vez, linkado em
#      módulos/protocolos diferentes ao mesmo tempo)
#
#   <XLXBRA  heard users>           → atividade recente (inclusive relayada)
#     <STATION>
#         <Callsign>PP5PK   </Callsign>      indicativo real do operador
#         <Via node>PP5MFA  B</Via node>     nó/gateway usado na conexão
#         <On module>D</On module>           módulo do refletor local
#         <Via peer>XLXBRA  </Via peer>      origem: local ou refletor linkado
#         <LastHeardTime>...</LastHeardTime>
#     </STATION>

RE_REFLECTOR = re.compile(r'<(\w+)\s+linked peers>')

RE_STATION = re.compile(
    r'<STATION>\s*'
    r'<Callsign>(.*?)</Callsign>\s*'
    r'<Via node>(.*?)</Via node>\s*'
    r'<On module>(.*?)</On module>\s*'
    r'<Via peer>(.*?)</Via peer>\s*'
    r'<LastHeardTime>(.*?)</LastHeardTime>\s*'
    r'</STATION>',
    re.DOTALL
)
RE_NODE = re.compile(
    r'<NODE>\s*'
    r'<Callsign>(.*?)</Callsign>\s*'
    r'<IP>.*?</IP>\s*'
    r'<LinkedModule>(.*?)</LinkedModule>\s*'
    r'<Protocol>(.*?)</Protocol>\s*'
    r'<ConnectTime>.*?</ConnectTime>\s*'
    r'<LastHeardTime>(.*?)</LastHeardTime>\s*'
    r'</NODE>',
    re.DOTALL
)
RE_XML_HORA = re.compile(r'(\d{1,2}:\d{2}:\d{2})')


def _fmt_cs(cs: str, sf) -> str:
    """Formata callsign: ('PP5PK', 'A') -> 'PP5PK-A' ; ('ECHO', None) -> 'ECHO'."""
    cs = cs.strip()
    return f"{cs}-{sf}" if sf and sf.strip() else cs


def _fmt_cs_xml(campo: str) -> str:
    """Formata um campo de indicativo do XML ('PP5MFA  B' -> 'PP5MFA-B')."""
    partes = campo.split()
    if not partes:
        return ''
    return _fmt_cs(partes[0], partes[1] if len(partes) > 1 else None)


def _xml_hora(ts_raw: str):
    """Extrai 'HH:MM:SS' de um LastHeardTime completo do XML."""
    m = RE_XML_HORA.search(ts_raw)
    hora = m.group(1) if m else None
    return (hora, hms_para_seg(hora)) if hora else ('--:--:--', -1)


def parse_xlxd_xml(xmlfile: str) -> dict:
    """
    Lê o XML de status do xlxd e retorna tudo que a tela precisa:

      reflector  : nome do refletor local (extraído de '<X  linked peers>')
      last_heard : lista de estações "heard", mais recentes primeiro
                   [{callsign, gateway, module, via_peer, hora}]
      clients    : lista de nós conectados agora, ordenada por gateway
                   [{gateway, module, protocol, hora}]
      error      : string de erro ou None

    Sem duração de TX — isso só existia no log de texto (não lido mais).
    O horário exibido é o próprio LastHeardTime do XML.
    """
    try:
        with open(xmlfile, 'r', encoding='utf-8', errors='replace') as f:
            conteudo = f.read()
    except OSError as e:
        logging.warning("Não foi possível ler %s: %s", xmlfile, e)
        return {'reflector': REFLECTOR_FALLBACK, 'last_heard': [], 'clients': [],
                'error': str(e)}

    m_ref = RE_REFLECTOR.search(conteudo)
    reflector = m_ref.group(1) if m_ref else REFLECTOR_FALLBACK

    last_heard = []
    for m in RE_STATION.finditer(conteudo):
        callsign_raw, via_node_raw, modulo_raw, via_peer_raw, ts_raw = m.groups()
        hora, _ = _xml_hora(ts_raw)
        last_heard.append({
            'callsign': callsign_raw.strip(),
            'gateway' : _fmt_cs_xml(via_node_raw),
            'module'  : modulo_raw.strip(),
            'via_peer': via_peer_raw.strip(),
            'hora'    : hora,
        })
    # O xlxd já escreve a seção "heard users" da mais recente pra mais
    # antiga — confia nessa ordem (não reordena por horário: o XML só tem
    # HH:MM:SS sem data, o que quebraria na virada do dia).
    last_heard = last_heard[:MAX_LASTHEARD]

    clients = []
    for m in RE_NODE.finditer(conteudo):
        callsign_raw, modulo_raw, proto_raw, ts_raw = m.groups()
        hora, _ = _xml_hora(ts_raw)
        clients.append({
            'gateway' : _fmt_cs_xml(callsign_raw),
            'module'  : modulo_raw.strip(),
            'protocol': proto_raw.strip(),
            'hora'    : hora,
        })
    clients.sort(key=lambda e: (e['gateway'], e['module']))

    return {'reflector': reflector, 'last_heard': last_heard,
            'clients': clients, 'error': None}


XLXD_SERVICE_PATHS = [
    "/etc/systemd/system/xlxd.service",
    "/lib/systemd/system/xlxd.service",
    "/usr/lib/systemd/system/xlxd.service",
]
RE_EXECSTART = re.compile(r'^ExecStart=\S+\s+(\S+)', re.MULTILINE)


def detectar_refletor_service(caminhos: list = XLXD_SERVICE_PATHS):
    """
    Lê o nome do refletor direto da unit do systemd do xlxd, ex.:
      ExecStart=/xlxd/xlxd XLXBRA 172.23.127.155 127.0.0.1  ->  'XLXBRA'

    Essa é a fonte mais confiável — não depende do XML já ter sido escrito
    nem de nenhuma seção específica estar presente nele. Usada como
    substituto do nome vindo do XML, quando disponível.

    Retorna None se nenhum dos caminhos candidatos existir/tiver o padrão
    (nesse caso o chamador cai de volta pro nome detectado no XML).
    """
    for caminho in caminhos:
        try:
            with open(caminho, 'r', encoding='utf-8', errors='replace') as f:
                conteudo = f.read()
        except OSError:
            continue
        m = RE_EXECSTART.search(conteudo)
        if m:
            return m.group(1)
    return None


class _SigTermRecebido(Exception):
    """Levantada pelo handler de SIGTERM — ver _handle_sigterm() abaixo."""
    pass


def _handle_sigterm(signum, frame):
    raise _SigTermRecebido()


# =============================================================================
#  MONITOR PRINCIPAL
# =============================================================================

class XLXMonitor:

    def __init__(self, simulate: bool = False, invert: bool = False) -> None:
        self.simulate    = simulate
        self.invert      = invert
        self._partial_ok = False

        self.data    = {'last_heard': [], 'clients': [], 'error': None}
        self.running = False
        self._lock       = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        self.fonte_hdr = carregar_fonte(13)   # cabeçalho
        self.fonte_lh  = carregar_fonte(8)   # last heard
        self.fonte_sm  = carregar_fonte(10)   # status / rodapé

        # Nome do refletor via xlxd.service — não muda em tempo de execução
        # (precisaria reiniciar o serviço), então detecta uma vez só aqui.
        self._reflector_service = detectar_refletor_service()
        if self._reflector_service:
            logging.info("Refletor detectado via xlxd.service: %s",
                         self._reflector_service)
        else:
            logging.info("xlxd.service não encontrado — nome do refletor virá do XML")
        self.data['reflector'] = self._reflector_service or REFLECTOR_FALLBACK

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

        # ── Corrige o pin factory do gpiozero ANTES do driver ser importado ──
        # O epdconfig.py da Waveshare instancia gpiozero.LED()/Button() já na
        # importação do módulo. O gpiozero abre chip=0 por padrão, mas no
        # Pi 5 o gpiochip do RP1 pode estar em outro número (varia com o
        # kernel/firmware). Detectamos o chip certo pelo rótulo e forçamos
        # o factory global antes de qualquer objeto gpiozero ser criado.
        try:
            import gpiozero
            from gpiozero.pins.lgpio import LGPIOFactory
            chip = detectar_gpiochip_rp1()
            gpiozero.Device.pin_factory = LGPIOFactory(chip=chip)
            logging.info("gpiozero pin factory: LGPIOFactory(chip=%d)", chip)
        except Exception as e:
            logging.warning("Não foi possível fixar o pin factory do gpiozero: %s", e)

        try:
            from waveshare_epd import epd2in13_V4 as epd_mod
            logging.info("Driver: epd2in13_V4 (HAT+ B/W)")
        except ImportError as e:
            logging.error("Driver epd2in13_V4 não encontrado: %s", e)
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

    # ── Thread de leitura do XML ──────────────────────────────────────────────

    def _refresh_data(self) -> None:
        logging.info("Thread XML iniciada (intervalo: %ds)", REFRESH_INTERVAL)
        while not self._stop_event.is_set():
            resultado = parse_xlxd_xml(XLX_XML)
            if self._reflector_service:
                resultado['reflector'] = self._reflector_service
            with self._lock:
                self.data = resultado
            n_lh  = len(resultado['last_heard'])
            n_cli = len(resultado['clients'])
            if resultado['error']:
                logging.warning("Erro ao ler XML: %s", resultado['error'])
            else:
                logging.debug("XML OK — %d last heard | %d clientes online",
                               n_lh, n_cli)
            for _ in range(REFRESH_INTERVAL):
                if self._stop_event.is_set():
                    break
                time.sleep(1)
        logging.info("Thread XML encerrada.")

    def start(self) -> None:
        self.running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._refresh_data,
                                        name='xml-worker', daemon=True)
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
            lh        = list(self.data.get('last_heard', []))
            cli       = list(self.data.get('clients',    []))
            err       = self.data.get('error', None)
            reflector = self.data.get('reflector', REFLECTOR_FALLBACK)

        agora = datetime.now()
        dia   = DIAS[agora.weekday()]

        bg = BLACK if self.invert else WHITE
        fg = WHITE if self.invert else BLACK

        img = Image.new('1', (W, H), bg)
        db  = ImageDraw.Draw(img)

        # ── CABEÇALHO — ancorado no topo da tela, aproveitando a folga interna
        # da fonte (o topo do texto tem ~4px de espaço em branco antes da
        # tinta visível, então subir a âncora não corta nada).
        db.text((3, -2), reflector, font=self.fonte_hdr, fill=fg)
        larg_ref = largura_texto(db, reflector, self.fonte_hdr)
        contador = f"[{len(cli):02d}]"
        db.text((3 + larg_ref + 4, -2), contador, font=self.fonte_hdr, fill=fg)
        ts_str    = agora.strftime('%H:%M')
        hdr_right = f"{dia} {agora.day:02d}/{agora.month:02d}  {ts_str}"
        larg_hdr  = largura_texto(db, hdr_right, self.fonte_hdr)
        db.text((W - larg_hdr - 2, -2), hdr_right, font=self.fonte_hdr, fill=fg)
        db.line([(0, Y_SEP1), (W - 1, Y_SEP1)], fill=fg)

        # ── RODAPÉ (clientes/gateways conectados) — calculado ANTES do last
        # heard, pra saber quanto espaço ele realmente precisa; o que sobrar
        # define quantas linhas de last heard cabem (ver mais abaixo).
        CLI_LINE_H  = 10    # altura de linha a 10pt
        CLI_SEP     = " / " # separador entre entradas (não mais dentro da entrada)
        GAP_LH_SEP  = 2     # gap entre a última linha de last heard e o separador (cálculo de capacidade)
        GAP_SEP_CLI = 1     # gap entre o separador e a primeira linha do rodapé (3px -> 1px)

        entradas_cli = [f"{c['gateway']} {c['protocol']}" for c in cli]
        larg_sep = largura_texto(db, CLI_SEP, self.fonte_sm)
        lines_cli: list[list[str]] = []
        if entradas_cli:
            row: list[str] = []
            row_w = 0
            for txt in entradas_cli:
                txt_w = largura_texto(db, txt, self.fonte_sm)
                add_w = txt_w + (larg_sep if row else 0)
                if row and row_w + add_w > W - 4:
                    lines_cli.append(row)
                    row, row_w = [txt], txt_w
                else:
                    row.append(txt)
                    row_w += add_w
            if row:
                lines_cli.append(row)
        n_linhas_cli = max(1, len(lines_cli))   # 1 linha mínima p/ "No clients"
        altura_cli   = n_linhas_cli * CLI_LINE_H

        # ── LAST HEARD ────────────────────────────────────────────────────────
        # Quantas linhas cabem = o que sobra depois de reservar a área do
        # rodapé (calculada acima) + separador + margens.
        espaco_livre  = H - Y_LH0 - altura_cli - GAP_LH_SEP - GAP_SEP_CLI
        n_lh_cabe     = max(0, espaco_livre // LH_STEP)

        if err and not lh:
            db.text((COL_CS, Y_LH0 + LH_STEP),
                    f"Erro: {err[:32]}", font=self.fonte_sm, fill=fg)
            n_lh_desenhadas = min(3, n_lh_cabe)   # espaço reservado p/ a msg
        elif not lh:
            # Sem atividade: centraliza mensagem na área reservada
            msg = "Sem atividade recente"
            lm  = largura_texto(db, msg, self.fonte_sm)
            n_lh_desenhadas = min(3, n_lh_cabe)
            db.text(((W - lm) // 2, Y_LH0 + (n_lh_desenhadas * LH_STEP) // 2),
                    msg, font=self.fonte_sm, fill=fg)
        else:
            n_lh_desenhadas = min(len(lh), n_lh_cabe)
            for i, tx in enumerate(lh[:n_lh_desenhadas]):
                y   = Y_LH0 + i * LH_STEP
                cs  = tx['callsign'][:8]
                gw  = tx['gateway']
                via_peer = tx.get('via_peer')
                gw_txt = f"{gw} via {via_peer}" if (via_peer and via_peer != reflector) else gw

                mod  = f"[{tx['module']}]"
                hora = tx['hora']
                larg_hora = largura_texto(db, hora, self.fonte_lh)
                larg_mod  = largura_texto(db, mod,  self.fonte_lh)
                x_hora = W - larg_hora - 2
                x_mod  = x_hora - MOD_HORA_GAP - larg_mod

                # Gateway usa todo o espaço entre sua coluna e o bloco
                # módulo+horário (que agora é alinhado à direita) —
                # truncamento por pixel só como rede de segurança.
                largura_gw = x_mod - COL_GW - 4
                gw_txt = truncar_para_largura(db, gw_txt, self.fonte_lh, largura_gw)

                db.text((COL_CS, y), cs,     font=self.fonte_lh, fill=fg)
                db.text((COL_GW, y), gw_txt, font=self.fonte_lh, fill=fg)
                db.text((x_mod,  y), mod,    font=self.fonte_lh, fill=fg)
                db.text((x_hora, y), hora,   font=self.fonte_lh, fill=fg)

        # ── Separador + rodapé, ancorados na BASE da tela ────────────────────────
        # Em vez de vir logo após as linhas de last heard desenhadas (o que
        # deixava sobra em branco no fim da tela quando havia poucos dados),
        # o rodapé fica sempre grudado no fundo — qualquer folga por falta
        # de dados aparece entre o last heard e o separador, não depois do
        # rodapé.
        cli_top = H - altura_cli
        sep2_y  = cli_top - GAP_SEP_CLI   # desceu 1px (era -1 extra)
        db.line([(0, sep2_y), (W - 1, sep2_y)], fill=fg)

        if not entradas_cli:
            db.text((2, cli_top), "No clients connected",
                    font=self.fonte_sm, fill=fg)
        else:
            for li, line in enumerate(lines_cli):
                y = cli_top + li * CLI_LINE_H
                if y + CLI_LINE_H > H:   # não ultrapassa o display
                    break
                db.text((2, y), CLI_SEP.join(line), font=self.fonte_sm, fill=fg)

        return img

    # ── Controle do display ───────────────────────────────────────────────────

    def _buf(self, img: Image.Image):
        return self.epd.getbuffer(img)

    def _do_full_refresh(self) -> None:
        img = self.renderizar()
        buf = self._buf(img)
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

    def _desenhar_tela_desligamento(self, reflector: str) -> Image.Image:
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

        # Nome do refletor centralizado (encolhe a fonte se não couber)
        while fonte_cs.size > 20 and largura_texto(db, reflector, fonte_cs) > W - 70 - 4:
            fonte_cs = carregar_fonte(fonte_cs.size - 4)
        larg = largura_texto(db, reflector, fonte_cs)
        db.text((70 + (W - 70 - larg) // 2, (H - fonte_cs.size) // 2),
                reflector, font=fonte_cs, fill=WHITE)

        db.line([(63, 10), (63, H - 10)], fill=WHITE, width=1)
        db.rectangle([1, 1, W - 2, H - 2], outline=WHITE)
        return img

    # ── Loop principal ────────────────────────────────────────────────────────

    def executar(self) -> None:
        logging.info("XLX Monitor | XML: %ds | limpeza: %ds | inversão: %ds",
                     REFRESH_INTERVAL, CLEAN_INTERVAL, INVERT_INTERVAL)
        # 'systemctl stop' manda SIGTERM, não SIGINT (Ctrl+C) — sem isso o
        # processo morria direto e a tela de desligamento nunca aparecia.
        signal.signal(signal.SIGTERM, _handle_sigterm)
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
                sleep_ate_proximo_minuto()
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

        except (KeyboardInterrupt, _SigTermRecebido) as e:
            motivo = "SIGTERM (systemctl stop)" if isinstance(e, _SigTermRecebido) else "Ctrl+C"
            logging.info("Interrompido: %s", motivo)
        finally:
            self.stop()
            logging.info("Exibindo tela de desligamento...")
            self.epd.init()
            self.epd.Clear()
            with self._lock:
                reflector = self.data.get('reflector', REFLECTOR_FALLBACK)
            img_bye = self._desenhar_tela_desligamento(reflector)
            buf_bye = self._buf(img_bye)
            self.epd.display(buf_bye)
            time.sleep(1)
            self.epd.sleep()
            self._epd_mod.epdconfig.module_exit(cleanup=True)

    def _loop_simulate(self) -> None:
        logging.info("Modo simulação — Ctrl+C para encerrar")
        try:
            while True:
                sleep_ate_proximo_minuto()
                img = self.renderizar()
                img.convert('RGB').save('/tmp/epd_preview.png')
                logging.info("→ /tmp/epd_preview.png  [%s]",
                             datetime.now().strftime('%H:%M:%S'))
        except KeyboardInterrupt:
            logging.info("Simulação encerrada.")

    def executar_uma_vez(self) -> None:
        resultado = parse_xlxd_xml(XLX_XML)
        if self._reflector_service:
            resultado['reflector'] = self._reflector_service
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

    def limpar_tela(self) -> None:
        """Deixa o display em branco (ou preto, com --black) e desliga o
        hardware — não roda o loop normal nem escreve nenhum dado."""
        bg = BLACK if self.invert else WHITE
        img = Image.new('1', (W, H), bg)

        if self.simulate:
            img.convert('RGB').save('/tmp/epd_preview.png')
            logging.info("Simulação (tela em branco) salva → /tmp/epd_preview.png")
            return

        self.epd.init()
        self.epd.Clear()
        self.epd.display(self._buf(img))
        time.sleep(1)
        self.epd.sleep()
        self._epd_mod.epdconfig.module_exit(cleanup=True)
        logging.info("Tela em branco — display em sleep.")


# =============================================================================
#  ENTRY POINT
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Monitor XLX — e-Paper WaveShare 2.13" HAT+ (epd2in13_V4)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  python3 xlx_monitor.py               # loop contínuo
  python3 xlx_monitor.py --black       # fundo preto
  python3 xlx_monitor.py --simulate    # preview PNG sem hardware
  python3 xlx_monitor.py --once        # atualiza uma vez e sai
  python3 xlx_monitor.py --off         # deixa a tela em branco e desliga
        """,
    )
    parser.add_argument('--simulate', action='store_true',
                        help='Renderiza em /tmp/epd_preview.png (sem hardware)')
    parser.add_argument('--once',     action='store_true',
                        help='Atualiza uma única vez e encerra')
    parser.add_argument('--black',    action='store_true',
                        help='Inverte as cores: fundo preto, texto branco')
    parser.add_argument('--off',      action='store_true',
                        help='Deixa a tela em branco (ou preta, com --black) e encerra')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S',
    )

    if args.black:
        logging.info("Modo invertido: fundo preto / texto branco")

    monitor = XLXMonitor(simulate=args.simulate, invert=args.black)
    if args.off:
        monitor.limpar_tela()
    elif args.once:
        monitor.executar_uma_vez()
    else:
        monitor.executar()


if __name__ == '__main__':
    main()
