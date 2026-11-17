"""Bounded, append-only assistant text; canonical output verifies the last message."""
from ask_bridge_process import executar_limitado as executar_limitado, FalhaStreaming


class ReconciliadorTexto:
    def __init__(self, emitir, max_bytes=16 * 1024 * 1024, max_segmentos=256):
        for limite in (max_bytes, max_segmentos):
            if type(limite) is not int or limite <= 0:
                raise FalhaStreaming("limite inválido")
        if not callable(emitir):
            raise FalhaStreaming("emissor inválido")
        self.emitir = emitir
        self.max_bytes = max_bytes
        self.max_segmentos = max_segmentos
        self.liberado = False
        self._segmentos = {}
        self._atual = None
        self._bytes = 0
        self._teve_texto = False
        self._fechado = False
        self._final = None

    @staticmethod
    def _validar_texto(texto):
        if not isinstance(texto, str):
            raise FalhaStreaming("texto inválido")
        try:
            return len(texto.encode("utf-8", "strict"))
        except UnicodeError as erro:
            raise FalhaStreaming("UTF-8 inválido") from erro

    def _aberto(self):
        if self._fechado:
            raise FalhaStreaming("stream encerrado")
        if not self.liberado:
            raise FalhaStreaming("modelo ainda não verificado")

    def liberar(self):
        if self._fechado:
            raise FalhaStreaming("stream encerrado")
        self.liberado = True

    def abrir_segmento(self, identidade):
        self._aberto()
        self._validar_texto(identidade)
        if not identidade or len(identidade) > 256 or "\0" in identidade:
            raise FalhaStreaming("identidade inválida")
        if identidade in self._segmentos:
            if identidade != self._atual:
                raise FalhaStreaming("mensagem histórica reaberta")
            return
        if len(self._segmentos) >= self.max_segmentos:
            raise FalhaStreaming("limite de mensagens excedido")
        self._segmentos[identidade] = ""
        self._atual = identidade

    @property
    def ultimo_texto(self):
        return self._segmentos.get(self._atual, "")

    def delta(self, texto):
        self._aberto()
        tamanho = self._validar_texto(texto)
        if self._atual is None:
            raise FalhaStreaming("mensagem ausente")
        if not texto:
            return
        separador = "\n\n" if not self.ultimo_texto and self._teve_texto else ""
        if self._bytes + tamanho + len(separador) > self.max_bytes:
            raise FalhaStreaming("limite de texto excedido")
        self.emitir(separador + texto)
        self._segmentos[self._atual] += texto
        self._bytes += tamanho + len(separador)
        self._teve_texto = True

    def snapshot(self, identidade, texto):
        self._aberto()
        self._validar_texto(identidade)
        self._validar_texto(texto)
        if identidade not in self._segmentos:
            self.abrir_segmento(identidade)
        anterior = self._segmentos[identidade]
        if texto == anterior:
            return
        if identidade != self._atual or not texto.startswith(anterior):
            raise FalhaStreaming("resposta reescrita durante o stream")
        self.delta(texto[len(anterior):])

    def finalizar(self, texto):
        self._validar_texto(texto)
        if self._fechado:
            if texto != self._final:
                raise FalhaStreaming("resposta final divergente")
            return
        self._aberto()
        if self._atual is None:
            self.abrir_segmento("final")
        anterior = self.ultimo_texto
        if texto and not anterior and any(texto == valor for valor in self._segmentos.values()):
            raise FalhaStreaming("resposta final ambígua")
        if not texto.startswith(anterior):
            raise FalhaStreaming("resposta final divergente")
        self.delta(texto[len(anterior):])
        self._final = texto
        self._fechado = True
