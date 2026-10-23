#!/usr/bin/env python
# -*- coding: utf-8 -*-

class Topic:
    def __init__(self, topic_id: int):
        self._topic_id = int(topic_id)

    def get_topic_id(self) -> int:
        return self._topic_id
