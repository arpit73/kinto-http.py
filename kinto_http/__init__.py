import collections
import logging
import uuid
from contextlib import contextmanager

import requests
from kinto_http import utils
from kinto_http.session import create_session, Session
from kinto_http.batch import BatchSession
from kinto_http.exceptions import (
    BucketNotFound,
    CollectionNotFound,
    KintoException,
    KintoBatchException,
)
from kinto_http.patch_type import PatchType, BasicPatch

logger = logging.getLogger("kinto_http")

__all__ = (
    "BearerTokenAuth",
    "Endpoints",
    "Session",
    "Client",
    "create_session",
    "BucketNotFound",
    "CollectionNotFound",
    "KintoException",
    "KintoBatchException",
)


OBJECTS_PERMISSIONS = {
    "bucket": ["group:create", "collection:create", "write", "read"],
    "group": ["write", "read"],
    "collection": ["write", "read", "record:create"],
    "record": ["read", "write"],
}

ID_FIELD = "id"
DO_NOT_OVERWRITE = {"If-None-Match": "*"}


class Endpoints(object):
    endpoints = {
        "root": "{root}/",
        "batch": "{root}/batch",
        "buckets": "{root}/buckets",
        "bucket": "{root}/buckets/{bucket}",
        "groups": "{root}/buckets/{bucket}/groups",
        "group": "{root}/buckets/{bucket}/groups/{group}",
        "collections": "{root}/buckets/{bucket}/collections",
        "collection": "{root}/buckets/{bucket}/collections/{collection}",
        "records": "{root}/buckets/{bucket}/collections/{collection}/records",  # NOQA
        "record": "{root}/buckets/{bucket}/collections/{collection}/records/{id}",  # NOQA
    }

    def __init__(self, root=""):
        self._root = root

    def get(self, endpoint, **kwargs):
        # Remove nullable values from the kwargs, and slugify the values.
        kwargs = dict((k, utils.slugify(v)) for k, v in kwargs.items() if v)

        try:
            pattern = self.endpoints[endpoint]
            return pattern.format(root=self._root, **kwargs)
        except KeyError as e:
            msg = "Cannot get {endpoint} endpoint, {field} is missing"
            raise KintoException(msg.format(endpoint=endpoint, field=",".join(e.args)))


class BearerTokenAuth(requests.auth.AuthBase):
    def __init__(self, token, type=None):
        self.token = token
        self.type = type or "Bearer"

    def __call__(self, r):
        r.headers["Authorization"] = "{} {}".format(self.type, self.token)
        return r


class Client(object):
    def __init__(
        self,
        *,
        server_url=None,
        session=None,
        auth=None,
        bucket="default",
        collection=None,
        retry=0,
        retry_after=None,
        ignore_batch_4xx=False
    ):
        self.endpoints = Endpoints()

        session_kwargs = dict(
            server_url=server_url, auth=auth, session=session, retry=retry, retry_after=retry_after
        )
        self.session = create_session(**session_kwargs)
        self._bucket_name = bucket
        self._collection_name = collection
        self._server_settings = None
        self._records_timestamp = {}
        self._ignore_batch_4xx = ignore_batch_4xx

    def clone(self, **kwargs):
        if "server_url" in kwargs or "auth" in kwargs:
            kwargs.setdefault("server_url", self.session.server_url)
            kwargs.setdefault("auth", self.session.auth)
        else:
            kwargs.setdefault("session", self.session)
        kwargs.setdefault("bucket", self._bucket_name)
        kwargs.setdefault("collection", self._collection_name)
        kwargs.setdefault("retry", self.session.nb_retry)
        kwargs.setdefault("retry_after", self.session.retry_after)
        return Client(**kwargs)

    @contextmanager
    def batch(self, **kwargs):
        if self._server_settings is None:
            resp, _ = self.session.request("GET", self.get_endpoint("root"))
            self._server_settings = resp["settings"]

        batch_max_requests = self._server_settings["batch_max_requests"]
        batch_session = BatchSession(
            self, batch_max_requests=batch_max_requests, ignore_4xx_errors=self._ignore_batch_4xx
        )
        batch_client = self.clone(session=batch_session, **kwargs)

        # Set a reference for reading results from the context.
        batch_client.results = batch_session.results

        yield batch_client
        batch_session.send()
        batch_session.reset()

    def get_endpoint(self, name, *, bucket=None, group=None, collection=None, id=None):
        """Return the endpoint with named parameters."""
        kwargs = {
            "bucket": bucket or self._bucket_name,
            "collection": collection or self._collection_name,
            "group": group,
            "id": id,
        }
        return self.endpoints.get(name, **kwargs)

    def _paginated(self, endpoint, records=None, *, if_none_match=None, pages=None, **kwargs):
        if records is None:
            records = collections.OrderedDict()
        headers = {}
        if if_none_match is not None:
            headers["If-None-Match"] = utils.quote(if_none_match)

        if pages is None:
            pages = 1 if "_limit" in kwargs else float("inf")

        record_resp, headers = self.session.request(
            "get", endpoint, headers=headers, params=kwargs
        )

        # Save the current records collection timestamp
        etag = headers.get("ETag", "").strip('"')
        self._records_timestamp[endpoint] = etag

        if record_resp:
            records_tuples = [(r["id"], r) for r in record_resp["data"]]
            records.update(collections.OrderedDict(records_tuples))

            if pages > 1 and "next-page" in map(str.lower, headers.keys()):
                # Paginated wants a relative URL, but the returned one is
                # absolute.
                next_page = headers["Next-Page"]
                return self._paginated(
                    next_page, records, if_none_match=if_none_match, pages=pages - 1
                )
        return list(records.values())

    def _get_cache_headers(self, safe, data=None, if_match=None):
        has_data = data is not None and data.get("last_modified")
        if if_match is None and has_data:
            if_match = data["last_modified"]
        if safe and if_match is not None:
            return {"If-Match": utils.quote(if_match)}
        # else return None

    def _extract_original_info(self, original, id, if_match):
        """Utility method to extract ID and last_modified.

        Many update methods require the ID of a resource (to generate
        a URL) and the last_modified to generate safe cache headers
        (If-Match).  As a convenience, we allow users to pass the
        original record retrieved from a get_* method, which also
        contains those values.  This utility method lets methods
        support both explicit arguments for ``id`` and ``if_match`` as
        well as implicitly reading them from an original resource.
        """
        if original:
            id = id or original.get("id")
            if_match = if_match or original.get("last_modified")

        return (id, if_match)

    def _patch_method(
        self, endpoint, patch, safe=True, if_match=None, data=None, permissions=None
    ):
        """Utility method for implementing PATCH methods."""
        if not patch:
            # Backwards compatibility: the changes argument was
            # introduced in 9.1.0, and covers both ``data`` and
            # ``permissions`` arguments. Support the old style of
            # passing dicts by casting them into a BasicPatch.
            patch = BasicPatch(data=data, permissions=permissions)

        if not isinstance(patch, PatchType):
            raise TypeError("couldn't understand patch body {}".format(patch))

        body = patch.body
        content_type = patch.content_type
        headers = self._get_cache_headers(safe, if_match=if_match) or {}
        headers["Content-Type"] = content_type

        resp, _ = self.session.request("patch", endpoint, payload=body, headers=headers)
        return resp

    def _create_if_not_exists(self, resource, **kwargs):
        try:
            create_method = getattr(self, "create_%s" % resource)
            return create_method(**kwargs)
        except KintoException as e:
            if not hasattr(e, "response") or e.response.status_code != 412:
                raise e
            # The exception contains the existing record in details.existing
            # but it's not enough as we also need to return the permissions.
            get_kwargs = {"id": kwargs["id"]}
            if resource in ("group", "collection", "record"):
                get_kwargs["bucket"] = kwargs["bucket"]

                if resource == "record":
                    get_kwargs["collection"] = kwargs["collection"]
                    _id = kwargs.get("id") or kwargs["data"]["id"]
                    get_kwargs["id"] = _id

            get_method = getattr(self, "get_%s" % resource)
            return get_method(**get_kwargs)

    def _delete_if_exists(self, resource, **kwargs):
        try:
            delete_method = getattr(self, "delete_%s" % resource)
            return delete_method(**kwargs)
        except KintoException as e:
            # Should not raise in case of a 404.
            should_raise = not (
                hasattr(e, "response") and e.response is not None and e.response.status_code == 404
            )

            # Should not raise in case of a 403 on a bucket.
            if should_raise and resource.startswith("bucket"):
                should_raise = not (
                    hasattr(e, "response")
                    and e.response is not None
                    and e.response.status_code == 403
                )
            if should_raise:
                raise e

    # Server Info

    def server_info(self):
        endpoint = self.get_endpoint("root")
        resp, _ = self.session.request("get", endpoint)
        return resp

    # Buckets

    def create_bucket(
        self, *, id=None, data=None, permissions=None, safe=True, if_not_exists=False
    ):

        if id is None and data:
            id = data.get("id", None)

        if if_not_exists:
            return self._create_if_not_exists(
                "bucket", id=id, data=data, permissions=permissions, safe=safe
            )
        headers = DO_NOT_OVERWRITE if safe else None
        endpoint = self.get_endpoint("bucket", bucket=id)

        logger.info("Create bucket %r" % id or self._bucket_name)

        resp, _ = self.session.request(
            "put", endpoint, data=data, permissions=permissions, headers=headers
        )
        return resp

    def update_bucket(self, *, id=None, data=None, permissions=None, safe=True, if_match=None):

        if id is None and data:
            id = data.get("id", None)

        endpoint = self.get_endpoint("bucket", bucket=id)
        headers = self._get_cache_headers(safe, data, if_match)

        logger.info("Update bucket %r" % id or self._bucket_name)

        resp, _ = self.session.request(
            "put", endpoint, data=data, permissions=permissions, headers=headers
        )
        return resp

    def patch_bucket(
        self,
        *,
        id=None,
        changes=None,
        data=None,
        original=None,
        permissions=None,
        safe=True,
        if_match=None
    ):
        """Issue a PATCH request on a bucket.

        :param changes: the patch to apply
        :type changes: PatchType
        :param original: the original bucket, from which the ID and
            last_modified can be taken
        :type original: dict
        """
        # Backwards compatibility: a dict is both a BasicPatch and a
        # possible bucket (this was the behavior in 9.0.1 and
        # earlier).  In other words, we consider the data as a
        # possible bucket, even though PATCH data probably shouldn't
        # also contain an ID or a last_modified, as these shouldn't be
        # modified by a user.
        original = original or data

        (id, if_match) = self._extract_original_info(original, id, if_match)
        endpoint = self.get_endpoint("bucket", bucket=id)
        logger.info("Patch bucket %r" % (id or self._bucket_name,))

        return self._patch_method(
            endpoint, changes, data=data, permissions=permissions, safe=safe, if_match=if_match
        )

    def get_buckets(self, **kwargs):
        endpoint = self.get_endpoint("buckets")
        return self._paginated(endpoint, **kwargs)

    def get_bucket(self, *, id=None, **kwargs):
        endpoint = self.get_endpoint("bucket", bucket=id)

        logger.info("Get bucket %r" % id or self._bucket_name)

        try:
            resp, _ = self.session.request("get", endpoint, params=kwargs)
        except KintoException as e:
            error_resp_code = e.response.status_code
            if error_resp_code == 401:
                msg = (
                    "Unauthorized. Please authenticate or make sure the bucket "
                    "can be read anonymously."
                )
                e = KintoException(msg, e)
                raise e

            raise BucketNotFound(id or self._bucket_name, e)
        return resp

    def delete_bucket(self, *, id=None, safe=True, if_match=None, if_exists=False):
        if if_exists:
            return self._delete_if_exists("bucket", id=id, safe=safe, if_match=if_match)
        endpoint = self.get_endpoint("bucket", bucket=id)
        headers = self._get_cache_headers(safe, if_match=if_match)

        logger.info("Delete bucket %r" % id or self._bucket_name)

        resp, _ = self.session.request("delete", endpoint, headers=headers)
        return resp["data"]

    def delete_buckets(self, *, safe=True, if_match=None):
        endpoint = self.get_endpoint("buckets")
        headers = self._get_cache_headers(safe, if_match=if_match)

        logger.info("Delete buckets")

        resp, _ = self.session.request("delete", endpoint, headers=headers)
        return resp["data"]

    # Groups

    def get_groups(self, *, bucket=None, **kwargs):
        endpoint = self.get_endpoint("groups", bucket=bucket)
        return self._paginated(endpoint, **kwargs)

    def create_group(
        self, *, id=None, bucket=None, data=None, permissions=None, safe=True, if_not_exists=False
    ):

        if id is None and data:
            id = data.get("id", None)

        if id is None:
            raise KeyError("Please provide a group id")

        if if_not_exists:
            return self._create_if_not_exists(
                "group", id=id, bucket=bucket, data=data, permissions=permissions, safe=safe
            )
        headers = DO_NOT_OVERWRITE if safe else None
        endpoint = self.get_endpoint("group", bucket=bucket, group=id)

        logger.info("Create group %r in bucket %r" % (id, bucket or self._bucket_name))

        try:
            resp, _ = self.session.request(
                "put", endpoint, data=data, permissions=permissions, headers=headers
            )
        except KintoException as e:
            if e.response.status_code == 403:
                msg = (
                    "Unauthorized. Please check that the bucket exists and "
                    "that you have the permission to create or write on "
                    "this group."
                )
                e = KintoException(msg, e)
            raise e

        return resp

    def update_group(
        self, *, id=None, bucket=None, data=None, permissions=None, safe=True, if_match=None
    ):

        if id is None and data:
            id = data.get("id", None)

        if id is None:
            raise KeyError("Please provide a group id")

        endpoint = self.get_endpoint("group", bucket=bucket, group=id)
        headers = self._get_cache_headers(safe, data, if_match)

        logger.info("Update group %r in bucket %r" % (id, bucket or self._bucket_name))

        resp, _ = self.session.request(
            "put", endpoint, data=data, permissions=permissions, headers=headers
        )
        return resp

    def patch_group(
        self,
        *,
        id=None,
        bucket=None,
        changes=None,
        data=None,
        original=None,
        permissions=None,
        safe=True,
        if_match=None
    ):
        """Issue a PATCH request on a bucket.

        :param changes: the patch to apply
        :type changes: PatchType
        :param original: the original bucket, from which the ID and
            last_modified can be taken
        :type original: dict
        """
        # Backwards compatibility: a dict is both a BasicPatch and a
        # possible bucket (this was the behavior in 9.0.1 and
        # earlier).  In other words, we consider the data as a
        # possible bucket, even though PATCH data probably shouldn't
        # also contain an ID or a last_modified, as these shouldn't be
        # modified by a user.
        original = original or data

        (id, if_match) = self._extract_original_info(original, id, if_match)
        endpoint = self.get_endpoint("group", bucket=bucket, group=id)
        logger.info("Patch group %r in bucket %r" % (id, bucket or self._bucket_name))

        return self._patch_method(
            endpoint, changes, data=data, permissions=permissions, safe=safe, if_match=if_match
        )

    def get_group(self, *, id, bucket=None):
        endpoint = self.get_endpoint("group", bucket=bucket, group=id)

        logger.info("Get group %r in bucket %r" % (id, bucket or self._bucket_name))

        resp, _ = self.session.request("get", endpoint)
        return resp

    def delete_group(self, *, id, bucket=None, safe=True, if_match=None, if_exists=False):
        if if_exists:
            return self._delete_if_exists(
                "group", id=id, bucket=bucket, safe=safe, if_match=if_match
            )
        endpoint = self.get_endpoint("group", bucket=bucket, group=id)
        headers = self._get_cache_headers(safe, if_match=if_match)

        logger.info("Delete group %r in bucket %r" % (id, bucket or self._bucket_name))

        resp, _ = self.session.request("delete", endpoint, headers=headers)
        return resp["data"]

    def delete_groups(self, *, bucket=None, safe=True, if_match=None):
        endpoint = self.get_endpoint("groups", bucket=bucket)
        headers = self._get_cache_headers(safe, if_match=if_match)

        logger.info("Delete groups in bucket %r" % bucket or self._bucket_name)

        resp, _ = self.session.request("delete", endpoint, headers=headers)
        return resp["data"]

    # Collections

    def get_collections(self, *, bucket=None, **kwargs):
        endpoint = self.get_endpoint("collections", bucket=bucket)
        return self._paginated(endpoint, **kwargs)

    def create_collection(
        self, *, id=None, bucket=None, data=None, permissions=None, safe=True, if_not_exists=False
    ):

        if id is None and data:
            id = data.get("id", None)

        if if_not_exists:
            return self._create_if_not_exists(
                "collection", id=id, bucket=bucket, data=data, permissions=permissions, safe=safe
            )

        headers = DO_NOT_OVERWRITE if safe else None
        endpoint = self.get_endpoint("collection", bucket=bucket, collection=id)

        logger.info(
            "Create collection %r in bucket %r"
            % (id or self._collection_name, bucket or self._bucket_name)
        )

        try:
            resp, _ = self.session.request(
                "put", endpoint, data=data, permissions=permissions, headers=headers
            )
        except KintoException as e:
            if e.response.status_code == 403:
                msg = (
                    "Unauthorized. Please check that the bucket exists and "
                    "that you have the permission to create or write on "
                    "this collection."
                )
                e = KintoException(msg, e)
            raise e

        return resp

    def update_collection(
        self, *, id=None, bucket=None, data=None, permissions=None, safe=True, if_match=None
    ):

        if id is None and data:
            id = data.get("id", None)

        endpoint = self.get_endpoint("collection", bucket=bucket, collection=id)
        headers = self._get_cache_headers(safe, data, if_match)

        logger.info(
            "Update collection %r in bucket %r"
            % (id or self._collection_name, bucket or self._bucket_name)
        )

        resp, _ = self.session.request(
            "put", endpoint, data=data, permissions=permissions, headers=headers
        )
        return resp

    def patch_collection(
        self,
        *,
        id=None,
        bucket=None,
        changes=None,
        data=None,
        original=None,
        permissions=None,
        safe=True,
        if_match=None
    ):
        """Issue a PATCH request on a collection.

        :param changes: the patch to apply
        :type changes: PatchType
        :param original: the original collection, from which the ID and
            last_modified can be taken
        :type original: dict
        """
        # Backwards compatibility: a dict is both a BasicPatch and a
        # possible collection (this was the behavior in 9.0.1 and
        # earlier).  In other words, we consider the data as a
        # possible collection, even though PATCH data probably shouldn't
        # also contain an ID or a last_modified, as these shouldn't be
        # modified by a user.
        original = original or data

        (id, if_match) = self._extract_original_info(original, id, if_match)
        endpoint = self.get_endpoint("collection", bucket=bucket, collection=id)
        logger.info(
            "Patch collection %r in bucket %r"
            % (id or self._collection_name, bucket or self._bucket_name)
        )

        return self._patch_method(
            endpoint, changes, data=data, permissions=permissions, safe=safe, if_match=if_match
        )

    def get_collection(self, *, id=None, bucket=None, **kwargs):
        endpoint = self.get_endpoint("collection", bucket=bucket, collection=id)

        logger.info(
            "Get collection %r in bucket %r"
            % (id or self._collection_name, bucket or self._bucket_name)
        )

        try:
            resp, _ = self.session.request("get", endpoint, params=kwargs)
        except KintoException as e:
            error_resp_code = e.response.status_code
            if error_resp_code == 404:
                raise CollectionNotFound(id or self._collection_name, e)
            raise
        return resp

    def delete_collection(
        self, *, id=None, bucket=None, safe=True, if_match=None, if_exists=False
    ):
        if if_exists:
            return self._delete_if_exists(
                "collection", id=id, bucket=bucket, safe=safe, if_match=if_match
            )
        endpoint = self.get_endpoint("collection", bucket=bucket, collection=id)
        headers = self._get_cache_headers(safe, if_match=if_match)

        logger.info(
            "Delete collection %r in bucket %r"
            % (id or self._collection_name, bucket or self._bucket_name)
        )

        resp, _ = self.session.request("delete", endpoint, headers=headers)
        return resp["data"]

    def delete_collections(self, *, bucket=None, safe=True, if_match=None):
        endpoint = self.get_endpoint("collections", bucket=bucket)
        headers = self._get_cache_headers(safe, if_match=if_match)

        logger.info("Delete collections in bucket %r" % bucket or self._bucket_name)

        resp, _ = self.session.request("delete", endpoint, headers=headers)
        return resp["data"]

    # Records

    def get_records_timestamp(self, *, collection=None, bucket=None, **kwargs):
        endpoint = self.get_endpoint("records", bucket=bucket, collection=collection)
        if endpoint not in self._records_timestamp:
            record_resp, headers = self.session.request("head", endpoint)

            # Save the current records collection timestamp
            etag = headers.get("ETag", "").strip('"')
            self._records_timestamp[endpoint] = etag

        return self._records_timestamp[endpoint]

    def get_records(self, *, collection=None, bucket=None, **kwargs):
        """Returns all the records"""
        endpoint = self.get_endpoint("records", bucket=bucket, collection=collection)
        return self._paginated(endpoint, **kwargs)

    def get_paginated_records(self, *, collection=None, bucket=None, **kwargs):
        endpoint = self.get_endpoint("records", bucket=bucket, collection=collection)

        return self._paginated_generator(endpoint, **kwargs)

    def _paginated_generator(self, endpoint, *, if_none_match=None, **kwargs):
        headers = {}
        if if_none_match is not None:
            headers["If-None-Match"] = utils.quote(if_none_match)

        record_resp, headers = self.session.request(
            "get", endpoint, headers=headers, params=kwargs
        )

        if record_resp:
            yield record_resp

        if "next-page" in map(str.lower, headers.keys()):
            next_page = headers["Next-Page"]
            yield from self._paginated_generator(next_page, if_none_match=if_none_match)

    def get_record(self, *, id, collection=None, bucket=None, **kwargs):
        endpoint = self.get_endpoint("record", id=id, bucket=bucket, collection=collection)

        logger.info(
            "Get record with id %r from collection %r in bucket %r"
            % (id, collection or self._collection_name, bucket or self._bucket_name)
        )

        resp, _ = self.session.request("get", endpoint, params=kwargs)
        return resp

    def create_record(
        self,
        *,
        id=None,
        bucket=None,
        collection=None,
        data=None,
        permissions=None,
        safe=True,
        if_not_exists=False
    ):

        id = id or data.get("id", None)
        if if_not_exists:
            return self._create_if_not_exists(
                "record",
                data=data,
                id=id,
                collection=collection,
                permissions=permissions,
                bucket=bucket,
                safe=safe,
            )
        id = id or str(uuid.uuid4())
        # Make sure that no record already exists with this id.
        headers = DO_NOT_OVERWRITE if safe else None

        endpoint = self.get_endpoint("record", id=id, bucket=bucket, collection=collection)

        logger.info(
            "Create record with id %r in collection %r in bucket %r"
            % (id, collection or self._collection_name, bucket or self._bucket_name)
        )

        try:
            resp, _ = self.session.request(
                "put", endpoint, data=data, permissions=permissions, headers=headers
            )
        except KintoException as e:
            if e.response.status_code == 403:
                msg = (
                    "Unauthorized. Please check that the collection exists "
                    "and that you have the permission to create or write on"
                    " this collection record."
                )
                e = KintoException(msg, e)
            raise e

        return resp

    def update_record(
        self,
        *,
        id=None,
        collection=None,
        bucket=None,
        data=None,
        permissions=None,
        safe=True,
        if_match=None
    ):
        id = id or data.get("id")
        if id is None:
            raise KeyError("Unable to update a record, need an id.")
        endpoint = self.get_endpoint("record", id=id, bucket=bucket, collection=collection)
        headers = self._get_cache_headers(safe, data, if_match)

        logger.info(
            "Update record with id %r in collection %r in bucket %r"
            % (id, collection or self._collection_name, bucket or self._bucket_name)
        )

        resp, _ = self.session.request(
            "put", endpoint, data=data, headers=headers, permissions=permissions
        )
        return resp

    def patch_record(
        self,
        *,
        id=None,
        collection=None,
        bucket=None,
        changes=None,
        data=None,
        original=None,
        permissions=None,
        safe=True,
        if_match=None
    ):
        """Issue a PATCH request on a record.

        :param changes: the patch to apply
        :type changes: PatchType
        :param original: the original record, from which the ID and
            last_modified can be taken
        :type original: dict
        """
        # Backwards compatibility: the data argument specifies both
        # changes to make to data, and a possible record (this was the
        # behavior in 9.0.1 and earlier).  In other words, we consider
        # the data as a possible record, even though PATCH data
        # probably shouldn't also contain an ID or a last_modified, as
        # these shouldn't be modified by a user.
        original = original or data

        (id, if_match) = self._extract_original_info(original, id, if_match)
        if id is None:
            raise KeyError("Unable to patch record, need an id.")

        endpoint = self.get_endpoint("record", id=id, bucket=bucket, collection=collection)

        logger.info(
            "Patch record with id %r in collection %r in bucket %r"
            % (id, collection or self._collection_name, bucket or self._bucket_name)
        )

        return self._patch_method(
            endpoint, changes, data=data, permissions=permissions, safe=safe, if_match=if_match
        )

    def delete_record(
        self, *, id, collection=None, bucket=None, safe=True, if_match=None, if_exists=False
    ):
        if if_exists:
            return self._delete_if_exists(
                "record", id=id, collection=collection, bucket=bucket, safe=safe, if_match=if_match
            )
        endpoint = self.get_endpoint("record", id=id, bucket=bucket, collection=collection)
        headers = self._get_cache_headers(safe, if_match=if_match)

        logger.info(
            "Delete record with id %r from collection %r in bucket %r"
            % (id, collection or self._collection_name, bucket or self._bucket_name)
        )

        resp, _ = self.session.request("delete", endpoint, headers=headers)
        return resp["data"]

    def delete_records(self, *, collection=None, bucket=None, safe=True, if_match=None):
        endpoint = self.get_endpoint("records", bucket=bucket, collection=collection)
        headers = self._get_cache_headers(safe, if_match=if_match)

        logger.info(
            "Delete records from collection %r in bucket %r"
            % (collection or self._collection_name, bucket or self._bucket_name)
        )

        resp, _ = self.session.request("delete", endpoint, headers=headers)
        return resp["data"]

    def __repr__(self):
        if self._collection_name:
            endpoint = self.get_endpoint(
                "collection", bucket=self._bucket_name, collection=self._collection_name
            )
        elif self._bucket_name:
            endpoint = self.get_endpoint("bucket", bucket=self._bucket_name)
        else:
            endpoint = self.get_endpoint("root")

        absolute_endpoint = utils.urljoin(self.session.server_url, endpoint)
        return "<KintoClient %s>" % absolute_endpoint
