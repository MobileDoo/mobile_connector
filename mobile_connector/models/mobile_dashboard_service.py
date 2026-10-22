"""Overview screen for managers and owners (API 1.18): figures that answer "how is the business doing today?".

Feature modules add cards by extending `_card_defs()` (which cards exist and who may see them) and defining the method
that builds each one, so only installed features show cards. Every figure is computed with the reader's own rights
(record rules and companies apply), so a director sees the whole company and a head of department only what the
rules let them read. A card whose model the user cannot read is left out, and one that fails never breaks the screen.
"""
import logging
from datetime import datetime, time, timedelta

import pytz
from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

TONES = ('neutral', 'good', 'warn', 'bad')


class MobileDashboardService(models.AbstractModel):
    _name = 'mobile.dashboard.service'
    _description = 'Mobile Overview'

    # ------------------------------------------------------------------
    # What cards exist (feature modules extend these)
    # ------------------------------------------------------------------

    @api.model
    def _sections(self):
        """[{key, label}] in display order: what needs the reader first, then the figures."""
        return [
            {'key': 'approvals', 'label': _('Waiting for you')},
            {'key': 'sales', 'label': _('Sales')},
            {'key': 'purchases', 'label': _('Purchases')},
            {'key': 'people', 'label': _('People')},
            {'key': 'stock', 'label': _('Stock and delivery')},
        ]

    @api.model
    def _card_defs(self):
        """[{key, section, feature, capability, build}]: `build` names the method returning the card (or None).

        `feature` / `capability` are the access the reader needs (the same as the home tiles).
        """
        return []

    # ------------------------------------------------------------------
    # The screen
    # ------------------------------------------------------------------

    @api.model
    def _mobile_dashboard(self):
        api_model = self.env['mobile.api']
        by_section = {}
        for definition in self._card_defs():
            if not api_model._mobile_can(definition['feature'], definition.get('capability')):
                continue
            try:
                with self.env.cr.savepoint():
                    card = getattr(self, definition['build'])()
            except Exception:  # one broken figure (a customised module...) must not blank the whole screen
                _logger.exception('Overview card %s failed', definition['key'])
                continue
            if card:
                card.update(key=definition['key'])
                by_section.setdefault(definition['section'], []).append(card)
        sections = [
            {'key': s['key'], 'label': s['label'], 'cards': by_section[s['key']]}
            for s in self._sections() if by_section.get(s['key'])
        ]
        return {
            'sections': sections,
            'generated_at': fields.Datetime.to_string(fields.Datetime.now()),
            'currency': self._currency_info(),
        }

    @api.model
    def _currency_info(self):
        currency = self.env.company.currency_id
        return {'symbol': currency.symbol, 'position': currency.position, 'decimals': currency.decimal_places}

    # ------------------------------------------------------------------
    # Helpers for the cards
    # ------------------------------------------------------------------

    @api.model
    def _can_read(self, model):
        if model not in self.env:
            return False
        Model = self.env[model]
        if hasattr(Model, 'has_access'):
            return Model.has_access('read')
        return Model.check_access_rights('read', raise_exception=False)  # Odoo 17

    @api.model
    def _count_card(self, model, domain, title, icon, color, tile=None, tone_if_any=None, compare=None, previous=None,
                    total=None):
        """A number of records. `tone_if_any`: the tone when the number is not zero (e.g. late deliveries: 'bad')."""
        if not self._can_read(model):
            return None
        value = self.env[model].search_count(domain)
        card = {'kind': 'count', 'title': title, 'icon': icon, 'color': color, 'tile': tile, 'value': value,
                'tone': tone_if_any if (tone_if_any and value) else 'neutral'}
        if total is not None:
            card['total'] = total
        if previous is not None:
            card.update(previous=previous, compare=compare)
        return card

    @api.model
    def _tz(self):
        return pytz.timezone(self.env.user.tz or 'UTC')

    @api.model
    def _local_day_start_utc(self, day):
        """Midnight of `day` (a date, in the reader's time zone) as a naive UTC datetime, the way Odoo stores it."""
        return self._tz().localize(datetime.combine(day, time.min)).astimezone(pytz.utc).replace(tzinfo=None)

    @api.model
    def _today(self):
        return fields.Date.context_today(self.with_context(tz=self.env.user.tz or 'UTC'))

    @api.model
    def _amount(self, model, domain, amount_field, currency_field='currency_id'):
        """Sum of an amount over the records, each currency converted to the company's at today's rate."""
        Model = self.env[model]
        company = self.env.company
        today = fields.Date.context_today(self)
        total = 0.0
        for currency, subtotal in Model._read_group(domain, groupby=[currency_field], aggregates=[f'{amount_field}:sum']):
            if not subtotal:
                continue
            total += currency._convert(subtotal, company.currency_id, company, today) if currency else subtotal
        return total

    @api.model
    def _amount_card(self, model, base_domain, date_field, amount_field, title, icon, color, period, tile=None,
                     trend_months=0):
        """An amount for the current day / month so far, and the same stretch before it (yesterday / last month)."""
        if not self._can_read(model):
            return None
        today = self._today()
        if period == 'day':
            start, previous_start, previous_end = today, today - timedelta(days=1), today
            end = today + timedelta(days=1)
        else:
            start = today.replace(day=1)
            previous_start = start - relativedelta(months=1)
            # The same number of days as elapsed this month, so day 10 is compared with the 10th days of last month.
            previous_end = min(previous_start + timedelta(days=(today - start).days + 1), start)
            end = today + timedelta(days=1)

        def span(first, last):
            return list(base_domain) + [(date_field, '>=', self._local_day_start_utc(first)),
                                        (date_field, '<', self._local_day_start_utc(last))]

        card = {
            'kind': 'amount', 'title': title, 'icon': icon, 'color': color, 'tile': tile, 'tone': 'neutral',
            'value': self._amount(model, span(start, end), amount_field),
            'previous': self._amount(model, span(previous_start, previous_end), amount_field),
            'compare': period,
        }
        if trend_months:
            first = start - relativedelta(months=trend_months - 1)
            months = [first + relativedelta(months=i) for i in range(trend_months)]
            card['trend'] = [{'start': fields.Date.to_string(m),
                              'value': self._amount(model, span(m, m + relativedelta(months=1)), amount_field)}
                             for m in months]
        return card
