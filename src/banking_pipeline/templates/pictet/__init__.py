"""Pictet-specific extraction templates.

Each Pictet advice document type that has a stable layout gets its own
template module here. The package's :data:`PICTET_TEMPLATES` tuple is what
:mod:`banking_pipeline.templates` consumes when assembling the global
``TEMPLATE_REGISTRY`` — register new templates by adding an instance to that
tuple.

Templates are organised by locale:

  - English (Luxembourg / Geneva): ``buy_etf``, ``buy_structured_products``,
    ``debit_of_fees``, ``dividend_notice``, ``final_redemption``,
    ``fx_forward``, ``incoming_payment``, ``interest_payment``,
    ``interest_scale``, ``internal_transfer``, ``limit_extension``,
    ``order_information_report``, ``payment``, ``redemption_notice``,
    ``settle_fx_forward``, ``spot``, ``subscription_notice``.
  - Spanish (Madrid branch): ``compra``, ``debito_de_gastos``, ``factura``,
    ``pago_interna``, ``reembolso``, ``reembolso_final``, ``suscripcion``,
    ``switch_entrada``, ``switch_salida``.
"""

from __future__ import annotations

from banking_pipeline.templates.pictet.buy_etf import PictetBuyEtfTemplate
from banking_pipeline.templates.pictet.buy_structured_products import (
    PictetBuyStructuredProductsTemplate,
)
from banking_pipeline.templates.pictet.compra import PictetCompraTemplate
from banking_pipeline.templates.pictet.debit_of_fees import PictetDebitOfFeesTemplate
from banking_pipeline.templates.pictet.debito_de_gastos import (
    PictetDebitoDeGastosTemplate,
)
from banking_pipeline.templates.pictet.dividend_notice import (
    PictetDividendNoticeTemplate,
)
from banking_pipeline.templates.pictet.factura import PictetFacturaTemplate
from banking_pipeline.templates.pictet.final_redemption import (
    PictetFinalRedemptionTemplate,
)
from banking_pipeline.templates.pictet.fx_forward import PictetFxForwardTemplate
from banking_pipeline.templates.pictet.incoming_payment import (
    PictetIncomingPaymentTemplate,
)
from banking_pipeline.templates.pictet.interest_payment import (
    PictetInterestPaymentTemplate,
)
from banking_pipeline.templates.pictet.interest_scale import (
    PictetInterestScaleTemplate,
)
from banking_pipeline.templates.pictet.internal_transfer import (
    PictetInternalTransferTemplate,
)
from banking_pipeline.templates.pictet.limit_extension import (
    PictetLimitExtensionTemplate,
)
from banking_pipeline.templates.pictet.order_information_report import (
    PictetOrderInformationReportTemplate,
)
from banking_pipeline.templates.pictet.pago_interna import (
    PictetPagoInternaTemplate,
)
from banking_pipeline.templates.pictet.payment import PictetPaymentTemplate
from banking_pipeline.templates.pictet.redemption_notice import (
    PictetRedemptionNoticeTemplate,
)
from banking_pipeline.templates.pictet.reembolso import PictetReembolsoTemplate
from banking_pipeline.templates.pictet.reembolso_final import (
    PictetReembolsoFinalTemplate,
)
from banking_pipeline.templates.pictet.settle_fx_forward import (
    PictetSettleFxForwardTemplate,
)
from banking_pipeline.templates.pictet.spot import PictetSpotTemplate
from banking_pipeline.templates.pictet.subscription_notice import (
    PictetSubscriptionNoticeTemplate,
)
from banking_pipeline.templates.pictet.suscripcion import (
    PictetSuscripcionTemplate,
)
from banking_pipeline.templates.pictet.switch_entrada import (
    PictetSwitchEntradaTemplate,
)
from banking_pipeline.templates.pictet.switch_salida import (
    PictetSwitchSalidaTemplate,
)

# Concrete template instances exposed to the global registry. Order is not
# significant — the registry is keyed on ``template_id`` — but keeping it
# alphabetical by template_id makes diffs easy to read.
PICTET_TEMPLATES: tuple[object, ...] = (
    PictetBuyEtfTemplate(),
    PictetBuyStructuredProductsTemplate(),
    PictetCompraTemplate(),
    PictetDebitOfFeesTemplate(),
    PictetDebitoDeGastosTemplate(),
    PictetDividendNoticeTemplate(),
    PictetFacturaTemplate(),
    PictetFinalRedemptionTemplate(),
    PictetFxForwardTemplate(),
    PictetIncomingPaymentTemplate(),
    PictetInterestPaymentTemplate(),
    PictetInterestScaleTemplate(),
    PictetInternalTransferTemplate(),
    PictetLimitExtensionTemplate(),
    PictetOrderInformationReportTemplate(),
    PictetPagoInternaTemplate(),
    PictetPaymentTemplate(),
    PictetRedemptionNoticeTemplate(),
    PictetReembolsoTemplate(),
    PictetReembolsoFinalTemplate(),
    PictetSettleFxForwardTemplate(),
    PictetSpotTemplate(),
    PictetSubscriptionNoticeTemplate(),
    PictetSuscripcionTemplate(),
    PictetSwitchEntradaTemplate(),
    PictetSwitchSalidaTemplate(),
)

__all__ = [
    "PICTET_TEMPLATES",
    "PictetBuyEtfTemplate",
    "PictetBuyStructuredProductsTemplate",
    "PictetCompraTemplate",
    "PictetDebitOfFeesTemplate",
    "PictetDebitoDeGastosTemplate",
    "PictetDividendNoticeTemplate",
    "PictetFacturaTemplate",
    "PictetFinalRedemptionTemplate",
    "PictetFxForwardTemplate",
    "PictetIncomingPaymentTemplate",
    "PictetInterestPaymentTemplate",
    "PictetInterestScaleTemplate",
    "PictetInternalTransferTemplate",
    "PictetLimitExtensionTemplate",
    "PictetOrderInformationReportTemplate",
    "PictetPagoInternaTemplate",
    "PictetPaymentTemplate",
    "PictetRedemptionNoticeTemplate",
    "PictetReembolsoTemplate",
    "PictetReembolsoFinalTemplate",
    "PictetSettleFxForwardTemplate",
    "PictetSpotTemplate",
    "PictetSubscriptionNoticeTemplate",
    "PictetSuscripcionTemplate",
    "PictetSwitchEntradaTemplate",
    "PictetSwitchSalidaTemplate",
]
