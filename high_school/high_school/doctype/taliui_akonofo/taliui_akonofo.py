import frappe
from frappe.model.document import Document
from high_school.high_school.attendance_utils import update_student_overall_moua_total

class TaliuiAkonofo(Document):
    def validate(self):
        punishment_rate = 2 

        if self.status == "Absent":
            self.houa_ngaue_moua = punishment_rate
        else:
            self.houa_ngaue_moua = 0

    def on_update(self):
        update_student_overall_moua_total(self.student)

    def on_trash(self):
        update_student_overall_moua_total(self.student)
