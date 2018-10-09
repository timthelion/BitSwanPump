import abc
import asyncio
import types
import logging
import itertools
import asab
from .abc.source import Source
from .abc.generator import Generator
from .abc.connection import Connection
from .exception import ProcessingError

#

L = logging.getLogger(__name__)

#

class Pipeline(abc.ABC):


	def __init__(self, app, id=None):

		self.Id = id if id is not None else self.__class__.__name__
		self.Loop = app.Loop

		self.Sources = []
		self.Processors = [[]] # List of lists of processors, the depth is increased by a Generator object
		self._source_coros = [] # List of source main() coroutines


		# Publish-Subscribe for this pipeline
		self.PubSub = asab.PubSub(app)
		metrics_service = app.get_service('asab.MetricsService')
		self.MetricsCounter = metrics_service.create_counter(
			"bspump.pipeline",
			tags={'pipeline':self.Id},
			init_values={
				'event.in': 0,
				'warning': 0,
				'error': 0,
			}
		)

		self._error = None # None if not in error state otherwise there is a tuple (exception, event)

		self._throttles = set()

		self._ready = asyncio.Event(loop = app.Loop)
		self._ready.clear()

		# Chillout is used to break a pipeline processing to smaller tasks that allows other event in event loop to be processed
		self._chillout_trigger = 10000
		self._chillout_counter = 0

		self._context = {}


	def is_error(self):
		return self._error is not None


	def set_error(self, context, event, exc):
		'''
		If called with `exc is None`, then reset error (aka recovery)
		'''

		if exc is None:
			# Reset branch
			if self._error is not None:
				self._error = None
				L.log(asab.LOG_NOTICE, "Error cleared at a pipeline '{}'".format(self.Id))
				self.PubSub.publish("bspump.pipeline.clear_error!", pipeline=self)
				self._evaluate_ready()

		else:
			if not self.catch_error(exc, event):
				self.MetricsCounter.add('warning', 1)
				self.PubSub.publish("bspump.pipeline.warning!", pipeline=self)
				return

				self.MetricsCounter.add('error', 1)

			if (self._error is not None):
				L.warning("Error on a pipeline is already set!")
			
			self._error = (context, event, exc)
			L.warning("Pipeline '{}' stopped due to a processing error: {} ({})".format(self.Id, exc, type(exc)))

			self.PubSub.publish("bspump.pipeline.error!", pipeline=self)
			self._evaluate_ready()


	def catch_error(self, exception, event):
		'''
		Override to evaluate on the pipeline processing error.
		Return True for hard errors (stop the pipeline processing) or False for soft errors that will be ignored 


class SampleInternalPipeline(bspump.Pipeline):

	def __init__(self, app, pipeline_id):
		super().__init__(app, pipeline_id)

		self.build(
			bspump.common.InternalSource(app, self),
			bspump.common.JSONParserProcessor(app, self),
			bspump.common.PPrintSink(app, self)
		)

	def catch_error(self, exception, event):
		if isinstance(exception, json.decoder.JSONDecodeError):
			return False
		return True

		'''
		return True


	def throttle(self, who, enable=True):
		L.warning("Pipeline '{}' throttle {}".format(self.Id, "enabled" if enable else "disabled"))
		if enable:
			self._throttles.add(who)
		else:
			self._throttles.remove(who)

		self._evaluate_ready()


	def _evaluate_ready(self):
		orig_ready = self.is_ready()

		# Do we observed an error?
		new_ready = self._error is None

		# Are we throttled?
		if new_ready:
			new_ready = len(self._throttles) == 0 

		if orig_ready != new_ready:
			if new_ready:
				self._ready.set()
				self.PubSub.publish("bspump.pipeline.ready!", pipeline=self)
			else:
				self._ready.clear()
				self.PubSub.publish("bspump.pipeline.not_ready!", pipeline=self)


	async def ready(self):
		'''
		Can be used in source: `await self.Pipeline.ready()`
		'''

		self._chillout_counter += 1
		if self._chillout_counter >= self._chillout_trigger:
			self._chillout_counter = 0
			await asyncio.sleep(0.0001, loop = self.Loop)

		await self._ready.wait()
		return True


	def is_ready(self):
		return self._ready.is_set()


	def _do_process(self, event, depth, context):

		for processor in self.Processors[depth]:
			try:
				event = processor.process(context, event)
			except BaseException as e:
				if depth > 0: raise # Handle error on the top level
				L.exception("Pipeline processing error in the '{}' on depth {}".format(self.Id, depth))
				self.set_error(context, event, e)
				raise

			if event is None: # Event has been consumed on the way
				return

		if event is None:
			return

		# If the event is generator and there is more in the processor pipeline, then enumerate generator
		elif isinstance(event, types.GeneratorType) and len(self.Processors) > depth:
			return event

		else:
			try:
				raise ProcessingError("Incomplete pipeline, event '{}' is not consumed by a Sink".format(event))
			except BaseException as e:
				L.exception("Pipeline processing error in the '{}' on depth {}".format(self.__class__.__name__, depth))
				self.set_error(context, event, e)
				raise


	async def process(self, event, context=None):
		while not self.is_ready():
			await self.ready()

		self.MetricsCounter.add('event.in', 1)

		if context is None:
			context = self._context.copy()
		else:
			context.update(self._context)

		gevent = self._do_process(event, depth=0, context=context)
		if gevent is not None:	
			await self._generator_process(gevent, 1, context=context)


	async def _generator_process(self, event, depth, context):
		for gevent in event:
			while not self.is_ready():
				await self.ready()
			
			ngevent = self._do_process(gevent, depth, context.copy())
			if ngevent is not None:
				self._generator_process(ngevent, depth+1, context)


	def locate_connection(self, app, connection_id):
		if isinstance(connection_id, Connection): return connection_id
		svc = app.get_service("bspump.PumpService")
		connection = svc.locate_connection(connection_id)
		if connection is None:
			raise RuntimeError("Cannot locate connection '{}'".format(connection_id))
		return connection


	# Construction

	def set_source(self, source):
		if isinstance(source, Source):
			self.Sources.append(source)
		else:
			self.Sources.extend(source)


	def append_processor(self, processor):
		#TODO: Check if possible: self.Processors[*][-1] is Sink, no processors after Sink, ...
		#TODO: Check if fitting
		self.Processors[-1].append(processor)

		if isinstance(processor, Generator):
			self.Processors.append([])


	def build(self, source, *processors):
		self.set_source(source)
		for processor in processors:
			self.append_processor(processor)


	def iter_processors(self):
		'''
		Iterate thru all processors.
		'''
		for processors in self.Processors:
			for processor in processors:
				yield processor


	# Lifecycle ...

	def start(self):
		self.PubSub.publish("bspump.pipeline.start!", pipeline=self)

		# Start all non-started sources
		for source in self.Sources:
			source.start(self.Loop)

		self._evaluate_ready()


	async def stop(self):
		# Stop all started sources
		for source in self.Sources:
			await source.stop()


	# Rest API

	def rest_get(self):
		rest = {
			'Id': self.Id,
			'Ready': self.is_ready(),
			'Sources': self.Sources,
			'Processors': [],
			'Metrics': self.MetricsCounter,
		}

		for l, processors in enumerate(self.Processors):
			rest['Processors'].append(processors)

		if self._error:
			error_text = str(self._error[2])
			if len(error_text) == 0:
				error_text = str(type(self._error[2]))
			rest['Error'] = error_text

		return rest
