// Copyright (c) 2024, Raymond Fung and contributors
// For license information, please see license.txt

frappe.ui.form.on("Shopify Integration Settings", {
	refresh(frm) {

	},

	sync_orders(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Please save your changes before syncing."));
			return;
		}

		frappe.call({
			method: "shopify_integration.shopify_selling.sync.enqueue_shopify_sync_orders",
			args: {
				doc: frm.doc.name,
				use_setting_date: true,
			},
			freeze: true,
			freeze_message: __("Queuing Shopify order sync..."),
			callback: function (r) {
				frappe.show_alert({
					message: __("Sync job queued. Check the Error Log or Sales Order list shortly."),
					indicator: "green",
				});
			},
		});
	},
});