import signal
import sys

import asab

from .service import BSPumpService
from .__version__ import __version__, __build__


class BSPumpApplication(asab.Application):


	"""
	-
	-

	BSPumpApplication is **class** used for .....

	-
	-

    init
--------




	"""

	def __init__(self, args=None, web_listen=None):
		'''
		Bude tohle nekde videt.
		'''
		super().__init__(args=args)

		# Banner
		print("BitSwan BSPump version {}".format(__version__))

		from asab.proactor import Module
		self.add_module(Module)

		from asab.metrics import Module
		self.add_module(Module)

		self.PumpService = BSPumpService(self)
		self.WebContainer = None

		try:
			# Signals are not available on Windows
			self.Loop.add_signal_handler(signal.SIGUSR1, self._on_signal_usr1)
		except (NotImplementedError, AttributeError):
			pass

		# Activate web frontend, if requested
		if web_listen is None:
			if self._web_listen is not None and len(self._web_listen) > 0:
				web_listen = self._web_listen
			elif "bspump:web" in asab.Config:
				web_listen = asab.Config["bspump:web"].get("listen", "")

		if web_listen is not None and len(web_listen) > 0:
			from .web import _initialize_web
			self.WebContainer = _initialize_web(self, web_listen)


	def create_argument_parser(self):
		'''
		create agrument parser is a method responsible for ....
		'''
		prog = sys.argv[0]
		if prog[-11:] == '__main__.py':
			prog = sys.executable + " -m bspump"

		description = '''
BSPump is a stream processor. It is a part of BitSwan.
For more information, visit: https://github.com/LibertyAces/BitSwanPump

version: {}
build: {} [{}]
'''.format(__version__, __build__, __build__[:7])


		parser = super().create_argument_parser(
			prog=prog,
			description=description
		)
		return parser


	def parse_arguments(self, args=None):
		'''
		parse arguments is a method which does....
		'''
		args = super().parse_arguments(args=args)
		self._web_listen = args.web_api
		return args


	async def main(self):
		print("{} pipeline(s) ready.".format(len(self.PumpService.Pipelines)))


	def _on_signal_usr1(self):
		'''
		To clear reset from all pipelines, run
		$ kill -SIGUSR1 xxxx
		Equivalently, you can use `docker kill -s SIGUSR1 ....` to reset containerized BSPump.
		'''
		# Reset errors from all pipelines
		for pipeline in self.PumpService.Pipelines.values():
			if not pipeline.is_error():
				continue  # Focus only on pipelines that has errors
			pipeline.set_error(None, None, None)
