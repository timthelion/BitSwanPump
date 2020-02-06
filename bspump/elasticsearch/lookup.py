import json
import logging

import requests

from ..abc.lookup import MappingLookup
from ..abc.lookup import AsyncLookupMixin
from ..cache import CacheDict

L = logging.getLogger(__name__)

# TODO: counters, cache dict?? serialization, doc, generator
class ElasticSearchLookup(MappingLookup, AsyncLookupMixin):

	"""
	The lookup that is linked with a ES.
	It provides a mapping (dictionary-like) interface to pipelines.
	It feeds lookup data from ES using a query.
	It also has a simple cache to reduce a number of datbase hits.

	**configs**

	*index* - Elastic's index

	*key* - field name to match

	*scroll_timeout* - Timeout of single scroll request (default is '1m'). Allowed time units:
	https://www.elastic.co/guide/en/elasticsearch/reference/current/common-options.html#time-units

	Example:

.. code:: python

	class ProjectLookup(bspump.elasticsearch.ElasticSearchLookup):

		async def count(self, database):
			return await database['projects'].count_documents({})

		def find_one(self, database, key):
			return database['projects'].find_one({'_id':key})

	"""

	ConfigDefaults = {
		'index': '',  # Specify an index
		'key': '',  # Specify field name to match
		'scroll_timeout': '1m',
	}

	def __init__(self, app, connection, id=None, config=None, cache=None):
		super().__init__(app, id=id, config=config)
		self.Connection = connection

		self.Index = self.Config['index']
		self.ScrollTimeout = self.Config['scroll_timeout']
		self.Key = self.Config['key']

		self.Count = -1
		if cache is None:
			self.Cache = CacheDict()
		else:
			self.Cache = cache

		metrics_service = app.get_service('asab.MetricsService')
		self.CacheCounter = metrics_service.create_counter("es.lookup.cache", tags={}, init_values={'hit': 0, 'miss': 0})
		self.SuccessCounter = metrics_service.create_counter("es.lookup.success", tags={}, init_values={'hit': 0, 'miss': 0})


	async def _find_one(self, key): #TODO
		prefix = '_search'
		request = {
			"size": 1,
			"query": self.build_find_one_query(key)
		}
		url = self.Connection.get_url() + '{}/{}'.format(self.Index, prefix)

		async with self.Connection.get_session() as session:
			async with session.post(
				url,
				json=request,
				headers={'Content-Type': 'application/json'}
			) as response:

				if response.status != 200:
					data = await response.text()
					L.error("Failed to fetch data from ElasticSearch: {} from {}\n{}".format(response.status, url, data))


				msg = await response.json()
				try:
					hit = data['hits']['hits'][0]
				except IndexError:
					return None

		return hit["_source"]


	async def get(self, key):
		"""
		Obtain the value from lookup asynchronously.
		"""

		try:
			value = self.Cache[key]
			self.CacheCounter.add('hit', 1)
		except KeyError:
			value = await self._find_one(key)
			self.Cache[key] = value
			self.CacheCounter.add('miss', 1)

		if value is None:
			self.SuccessCounter.add('miss', 1)
		else:
			self.SuccessCounter.add('hit', 1)

		return value


	def build_find_one_query(self, key) -> dict:
		"""
		Override this method to build your own lookup query
		:return: Default single-key query
		"""
		return {
			'match': {
				self.Key: key
			}
		}


	async def _count(self):
		prefix = "_count"
		request = {
			"query": {
				"match_all": {}
			}
		}

		url = self.Connection.get_url() + '{}/{}'.format(self.Index, prefix)

		async with self.Connection.get_session() as session:
			async with session.post(
				url,
				json=request,
				headers={'Content-Type': 'application/json'}
			) as response:

				if response.status != 200:
					data = await response.text()
					L.error("Failed to fetch data from ElasticSearch: {} from {}\n{}".format(response.status, url, data))


				msg = await response.json()

		return int(msg["count"])


	async def load(self):
		self.Count = len(self.Cache)
		return True


	def __len__(self):
		return self.Count


	def __getitem__(self, key):
		# To avoid synchronous operations completely
		raise NotImplementedError()


	def __iter__(self):
		scroll_id = None
		request = {
			"size": 10000,
			"query": {
				"match_all": {}
			}
		}

		all_hits = []
		while True:
			if scroll_id is None:
				path = '{}/_search?scroll={}'.format(self.Index, self.ScrollTimeout)
				request_body = request
			else:
				path = "_search/scroll"
				request_body = {"scroll": self.ScrollTimeout, "scroll_id": scroll_id}

			url = self.Connection.get_url() + path
			response = requests.post(url, json=request_body)

			if response.status_code != 200:
				data = response.text()
				L.error("Failed to fetch data from ElasticSearch: {} from {}\n{}".format(response.status_code, url, data))
				break

			data = json.loads(response.text)

			scroll_id = data.get('_scroll_id')

			if scroll_id is None:
				break

			hits = data['hits']['hits']

			if len(hits) == 0:
				break

			all_hits.extend(hits)

		self.Iterator = all_hits.__iter__()
		return self


	def __next__(self):
		element = next(self.Iterator)

		key = element['_source'].get(self.Key)
		if key is not None:
			self.Cache[key] = element['_source']
		return key


	def serialize(self):
		return (json.dumps(dict(self.Cache))).encode('utf-8')

	
	def deserialize(self, data):
		new_cache = json.loads(data.decode('utf-8'))
		old_cache = dict(self.Cache)
		old_cache.update(new_cache)
		self.Cache = CacheDict(old_cache)

	# REST

	def rest_get(self):
		rest = super().rest_get()
		rest["Cache"] = dict(self.Cache)
		return rest


	@classmethod
	def construct(cls, app, definition: dict):
		newid = definition.get('id')
		config = definition.get('config')
		connection = definition['args']['connection']
		return cls(app, newid, connection, config)
