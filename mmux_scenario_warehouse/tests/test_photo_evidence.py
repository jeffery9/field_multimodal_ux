# -*- coding: utf-8 -*-
"""Photo evidence: the `photos` parameter must become real attachments.

PRD §2.6 — a worker photographs the job, and the photo is the evidence.
The thing being tested is not "does a base64 string survive a round trip"
but that the photo ends up somewhere a colleague can actually find it six
months later, filed against the record it belongs to and reachable from
the chatter message that describes the work.

Three properties carry the weight here, and each has a failure mode that
looks fine in a UI:

1. **The attachment is on the RECORD, not on the mail message only.**
   `res_model`/`res_id` are what make it show up under the record's
   attachments.  Without them the photo exists but is orphaned — visible
   from the chatter of one message and nowhere else.

2. **A call with no photos still works.**  Every existing caller passes
   only a description, so the parameter is optional.  A regression here
   would break the field workflow for every worker on a device without a
   camera — the population least able to report it.

3. **The write still travels the guard.**  `photos` is an ordinary
   capability parameter, so a user without write access must still be
   refused.  If the attachment path bypassed the check, a photographed
   record would become a way to write to a record the user cannot write.
"""
import base64

from odoo.tests import TransactionCase, tagged

# A 1x1 JPEG.  Real bytes, not a placeholder string: `ir.attachment`
# resolves the mimetype and size from the data, so a fake payload would
# exercise a different code path than a phone camera would.
TINY_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8U"
    "HRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA"
    "/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAAPwA="
)


@tagged("post_install", "-at_install")
class TestPickingPhotoEvidence(TransactionCase):
    """Warehouse: `picking_capture_evidence` with photos."""

    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]
        self.partner = self.env["res.partner"].create({"name": "MMUX 收货方"})
        self.picking = self.env["stock.picking"].create({
            "partner_id": self.partner.id,
            "picking_type_id": self.env.ref("stock.picking_type_in").id,
        })

    def _photos(self, count=2):
        return [{"image_b64": TINY_JPEG_B64,
                 "name": "site_%s.jpg" % i,
                 "mimetype": "image/jpeg"}
                for i in range(count)]

    def test_photos_become_attachments_on_the_picking(self):
        res = self.service.execute_capability(
            "picking_capture_evidence",
            {"note": "到货外观检查", "photos": self._photos(2)},
            record_id=self.picking.id, idem_key="mmux:ph-w1")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["result"]["photo_count"], 2)

        attachments = self.env["ir.attachment"].search([
            ("res_model", "=", "stock.picking"),
            ("res_id", "=", self.picking.id),
        ])
        self.assertEqual(len(attachments), 2,
                         "each photo must be filed against the record "
                         "itself, not only against the message")
        for att in attachments:
            self.assertEqual(att.mimetype, "image/jpeg")
            self.assertTrue(att.datas,
                            "the attachment must carry the image bytes")

    def test_the_message_references_the_attachments(self):
        """The chatter message is the human-readable index of the photos."""
        res = self.service.execute_capability(
            "picking_capture_evidence",
            {"note": "包装破损", "photos": self._photos(1)},
            record_id=self.picking.id, idem_key="mmux:ph-w2")
        message = self.env["mail.message"].browse(
            res["result"]["message_id"])
        self.assertTrue(message.attachment_ids,
                        "the posted message must carry the photo")
        self.assertEqual(message.attachment_ids[0].res_id, self.picking.id,
                         "and it must still be filed against the record, "
                         "not against the message alone")

    def test_no_photos_still_posts_the_note(self):
        """Backwards compatibility: every pre-existing caller omits
        `photos`, and a device without a camera must keep working."""
        res = self.service.execute_capability(
            "picking_capture_evidence", {"note": "只写备注"},
            record_id=self.picking.id, idem_key="mmux:ph-w3")
        self.assertEqual(res["status"], "success")
        self.assertNotIn("photo_count", res["result"])
        self.assertEqual(self.env["ir.attachment"].search_count([
            ("res_model", "=", "stock.picking"),
            ("res_id", "=", self.picking.id)]), 0)

    def test_a_photo_with_no_data_is_skipped_not_fatal(self):
        """A camera that returned nothing must not lose the report it was
        attached to — the worker's note is the part they cannot retype."""
        res = self.service.execute_capability(
            "picking_capture_evidence",
            {"note": "照片上传失败但备注保留",
             "photos": [{"image_b64": "", "name": "broken.jpg"},
                        {"image_b64": TINY_JPEG_B64, "name": "ok.jpg"}]},
            record_id=self.picking.id, idem_key="mmux:ph-w4")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["result"]["photo_count"], 1)
        self.assertIn("备注保留", self.picking.message_ids[0].body)

    def test_a_data_url_is_accepted_as_well_as_bare_base64(self):
        """`FileReader` hands back a data URL; the server tolerates it so a
        client that forgets to strip the header is not a silent data loss.

        Counting the attachment is not enough to prove this -- the prefix
        has to be GONE from the stored bytes.  If it stayed, the row would
        still exist and the count would still be 1, but the file would be
        a text stub whose first bytes are `data:image/jpeg;base64,` and no
        viewer would open it.  So decode and check the magic number.
        """
        res = self.service.execute_capability(
            "picking_capture_evidence",
            {"note": "数据 URL",
             "photos": [{"image_b64":
                         "data:image/jpeg;base64," + TINY_JPEG_B64}]},
            record_id=self.picking.id, idem_key="mmux:ph-w5")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["result"]["photo_count"], 1)
        att = self.env["ir.attachment"].browse(
            res["result"]["attachment_ids"][0])
        self.assertEqual(base64.b64decode(att.datas)[:3].hex(), "ffd8ff",
                         "the data-URL header must be stripped, not stored")

    def test_the_guard_still_applies_when_photos_are_present(self):
        """Photos must not become a way around the access check: the
        capability is the same funnel, so a user without write access is
        refused exactly as they would be without a camera.

        Two assertions, and the second is the one that makes this a real
        test.  Refusing the call is necessary; leaving no attachment is
        what proves the refusal happened BEFORE the write, not after it.
        A guard that checked rights only for the message would still pass
        a status check while having already filed the photo.
        """
        self.assertEqual(self.env["ir.attachment"].search_count([
            ("res_model", "=", "stock.picking")]), 0,
            "precondition: nothing is attached yet")
        outsider = self.env["res.users"].create({
            "name": "MMUX 无权限用户",
            "login": "mmux_no_access_photo",
            "email": "mmux_no_access_photo@example.com",
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
        })
        res = self.service.with_user(outsider).execute_capability(
            "picking_capture_evidence",
            {"note": "越权尝试", "photos": self._photos(1)},
            record_id=self.picking.id, idem_key="mmux:ph-w6")
        self.assertNotEqual(res["status"], "success")
        # The refusal must come from the ACCESS CHECK, not from a later
        # failure.  If `check_access_rights` were removed, the photo write
        # would still be caught by the attachment layer -- so a bare
        # "not success" would pass either way, and the guard could rot
        # unnoticed behind its own backstop.
        self.assertIn("access to this record",
                      (res.get("error") or "").lower()
                      + str(res.get("details") or "").lower(),
                      "the refusal must be the guard's access check, not a "
                      "downstream side effect")
        self.assertEqual(self.env["ir.attachment"].search_count([
            ("res_model", "=", "stock.picking"),
            ("res_id", "=", self.picking.id)]), 0,
            "a refused call must not leave a photo behind")
        self.assertEqual(self.env["ir.attachment"].search_count([]), 0,
                         "nor an orphan with no record at all")


@tagged("post_install", "-at_install")
class TestServicePhotoEvidence(TransactionCase):
    """Field service: `service_capture_evidence` with photos."""

    def setUp(self):
        super().setUp()
        self.service = self.env["mmux.service"]
        self.project = self.env["project.project"].create({
            "name": "MMUX 售后"})
        self.task = self.env["project.task"].create({
            "name": "MMUX 检修", "project_id": self.project.id})

    def test_photos_are_filed_against_the_task(self):
        res = self.service.execute_capability(
            "service_capture_evidence",
            {"description": "维修前后",
             "photos": [{"image_b64": TINY_JPEG_B64, "name": "before.jpg"},
                        {"image_b64": TINY_JPEG_B64, "name": "after.jpg"}]},
            record_id=self.task.id, idem_key="mmux:ph-s1")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["result"]["photo_count"], 2)
        attachments = self.env["ir.attachment"].search([
            ("res_model", "=", "project.task"),
            ("res_id", "=", self.task.id),
        ])
        self.assertEqual(len(attachments), 2)
        self.assertEqual(sorted(attachments.mapped("name")),
                         ["after.jpg", "before.jpg"])

    def test_description_only_still_posts(self):
        res = self.service.execute_capability(
            "service_capture_evidence", {"description": "仅文字证据"},
            record_id=self.task.id, idem_key="mmux:ph-s2")
        self.assertEqual(res["status"], "success")
        self.assertNotIn("photo_count", res["result"])

    def test_the_attached_bytes_are_the_bytes_we_sent(self):
        """Decoding the stored attachment must give back what was
        uploaded — a truncation bug would still produce a row."""
        res = self.service.execute_capability(
            "service_capture_evidence",
            {"description": "原图校验",
             "photos": [{"image_b64": TINY_JPEG_B64, "name": "raw.jpg"}]},
            record_id=self.task.id, idem_key="mmux:ph-s3")
        att = self.env["ir.attachment"].browse(
            res["result"]["attachment_ids"][0])
        self.assertEqual(base64.b64decode(att.datas)[:3].hex(), "ffd8ff",
                         "stored bytes must still start with a JPEG magic "
                         "number (0xFFD8FF)")
