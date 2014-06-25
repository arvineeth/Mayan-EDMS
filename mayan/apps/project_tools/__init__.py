from __future__ import absolute_import

from django.utils.translation import ugettext_lazy as _

from navigation.api import register_top_menu

from .links import link_tools

tool_link = register_top_menu('tools', link=link_tools, position=-3)
