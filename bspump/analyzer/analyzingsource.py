import logging

from ..abc.source import TriggerSource


L = logging.getLogger(__name__)


class AnalyzingSource(TriggerSource):
	'''
	The `AnalyzingSource` is triggered source, which expects `matrix_id` as an input.
	Each trigger fire it calls `analyze()` function of the `Matrix` and expects
	a complex event as an output. A complex event can be array of events, aggregation of events (average, max, min etc.)
	'''


	def __init__(self, app, pipeline, matrix_id, id=None, config=None):
		"""
		Description:

		**Parameters**

		app : Application
			Name of the Application

		pipeline : Pipeline
			Name of the Pipeline

		matrix_id : str
			ID of the matrix.

		id : str, default = None
			ID

		config : JSON, default = None
			configuration file containing additional information.


		"""
		super().__init__(app, pipeline, id=id, config=config)
		svc = app.get_service("bspump.PumpService")
		self.AnalyzeMatrix = svc.locate_matrix(matrix_id)



	async def cycle(self):
		"""
		Description:

		"""
		event = await self.AnalyzeMatrix.analyze()
		await self.process(event)
