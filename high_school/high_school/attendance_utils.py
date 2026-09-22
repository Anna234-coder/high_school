import frappe
from frappe import _
from frappe.utils import add_days, getdate


# ---------------------------------------------------------------------------
# STANDARD STUDENT ATTENDANCE
# ---------------------------------------------------------------------------

def mark_standard_attendance(
    student,
    student_name,
    status,
    course_schedule=None,
    student_group=None,
    date=None,
):
    """
    Create or update a Student Attendance record.

    Existing approved Leave attendance is preserved.
    """

    course_schedule_filter = (
        course_schedule
        if course_schedule
        else ["is", "not set"]
    )

    existing_name = frappe.db.exists(
        "Student Attendance",
        {
            "student": student,
            "course_schedule": course_schedule_filter,
            "student_group": student_group,
            "date": date,
            "docstatus": ["<", 2],
        },
    )

    if existing_name:
        current_status = frappe.db.get_value(
            "Student Attendance",
            existing_name,
            "status",
        )

        # Do not overwrite approved leave with Present/Absent.
        if current_status == "Leave":
            return existing_name

        frappe.db.set_value(
            "Student Attendance",
            existing_name,
            "status",
            status,
        )

        return existing_name

    attendance = frappe.new_doc("Student Attendance")

    attendance.student = student
    attendance.student_name = student_name
    attendance.course_schedule = course_schedule
    attendance.student_group = student_group
    attendance.date = date
    attendance.status = status

    attendance.insert(
        ignore_permissions=True,
        ignore_mandatory=True,
    )

    attendance.submit()

    return attendance.name


# ---------------------------------------------------------------------------
# TALIUI ATTENDANCE
# ---------------------------------------------------------------------------

def mark_taliui_attendance_records(
    students,
    house,
    taliui,
    date,
):
    """Create or update Taliui Akonofo records."""

    for student in students:
        student_id = student["student"]

        status = (
            "Present"
            if student.get("checked")
            else "Absent"
        )

        existing_name = frappe.db.get_value(
            "Taliui Akonofo",
            {
                "student": student_id,
                "date": date,
                "taliui": taliui,
            },
        )

        if existing_name:
            frappe.db.set_value(
                "Taliui Akonofo",
                existing_name,
                "status",
                status,
            )

            continue

        frappe.get_doc(
            {
                "doctype": "Taliui Akonofo",
                "student": student_id,
                "taliui": taliui,
                "date": date,
                "status": status,
                "house": house,
            }
        ).insert()


# ---------------------------------------------------------------------------
# LEAVE → ATTENDANCE
# ---------------------------------------------------------------------------

def update_attendance_on_leave_approval(doc, method=None):
    """
    When a Student Leave Application is approved,
    create/update attendance records for all applicable
    Course Schedules during the leave period.
    """

    student_groups = frappe.get_all(
        "Student Group Student",
        filters={
            "student": doc.student,
            "active": 1,
        },
        pluck="parent",
    )

    if not student_groups:
        return

    current_date = getdate(doc.from_date)
    end_date = getdate(doc.to_date)

    while current_date <= end_date:

        schedules = frappe.get_all(
            "Course Schedule",
            filters={
                "student_group": ["in", student_groups],
                "schedule_date": current_date,
            },
            fields=[
                "name",
                "student_group",
            ],
        )

        for schedule in schedules:

            existing = frappe.db.exists(
                "Student Attendance",
                {
                    "student": doc.student,
                    "course_schedule": schedule.name,
                    "date": current_date,
                    "docstatus": ["<", 2],
                },
            )

            if existing:
                frappe.db.set_value(
                    "Student Attendance",
                    existing,
                    {
                        "status": "Leave",
                        "leave_application": doc.name,
                        "student_group": schedule.student_group,
                    },
                )

                continue

            attendance = frappe.get_doc(
                {
                    "doctype": "Student Attendance",
                    "student": doc.student,
                    "student_name": doc.student_name,
                    "date": current_date,
                    "status": "Leave",
                    "student_group": schedule.student_group,
                    "course_schedule": schedule.name,
                    "leave_application": doc.name,
                }
            )

            attendance.insert(ignore_permissions=True)
            attendance.submit()

        current_date = add_days(current_date, 1)


# ---------------------------------------------------------------------------
# PUNISHMENT
# ---------------------------------------------------------------------------

def process_standard_attendance_punishment(
    doc,
    method=None,
):
    """
    Apply punishment hours to Absent attendance when
    the Education Settings switch is enabled.
    """

    apply_punishment = frappe.db.get_single_value(
        "Education Settings",
        "custom_apply_attendance_punishment",
    )

    if apply_punishment and doc.status == "Absent":
        doc.custom_houa_ngaue_moua = 2
    else:
        doc.custom_houa_ngaue_moua = 0


def update_student_overall_moua_total(student):
    """Recalculate the student's total MOUA/punishment hours."""

    if not student:
        return

    taliui_total = (
        frappe.db.sql(
            """
            SELECT SUM(houa_ngaue_moua)
            FROM `tabTaliui Akonofo`
            WHERE student = %s
            """,
            student,
        )[0][0]
        or 0
    )

    standard_total = 0

    if frappe.db.has_column(
        "Student Attendance",
        "custom_houa_ngaue_moua",
    ):
        standard_total = (
            frappe.db.sql(
                """
                SELECT SUM(custom_houa_ngaue_moua)
                FROM `tabStudent Attendance`
                WHERE student = %s
                AND docstatus < 2
                """,
                student,
            )[0][0]
            or 0
        )

    total = taliui_total + standard_total

    frappe.db.set_value(
        "Student",
        student,
        "custom_total_moua",
        total,
        update_modified=False,
    )


def trigger_standard_attendance_recalc(
    doc,
    method=None,
):
    """Recalculate the student's total after attendance changes."""

    update_student_overall_moua_total(doc.student)
def notify_guardian_on_attendance(doc, method=None):
    """
    Send an attendance notification to all parents/guardians
    linked to the student.

    Notifications are sent only for Absent or Leave attendance.
    """

    # ---------------------------------------------------------
    # Only notify for Absent or Leave
    # ---------------------------------------------------------

    if doc.status not in ("Absent", "Leave"):
        return None

    if not doc.student:
        return None

    # ---------------------------------------------------------
    # Get all guardians linked to the Student
    # ---------------------------------------------------------

    guardians = frappe.get_all(
        "Student Guardian",
        filters={
            "parent": doc.student,
            "parenttype": "Student",
            "parentfield": "guardians",
        },
        fields=["guardian", "guardian_name"],
    )

    if not guardians:
        frappe.log_error(
            f"No guardians found for Student {doc.student}",
            "Attendance Guardian Notification",
        )
        return None

    recipient_emails = []
    guardian_names = []

    # ---------------------------------------------------------
    # Get each Guardian's email address
    # ---------------------------------------------------------

    for row in guardians:

        if not row.guardian:
            continue

        email = frappe.db.get_value(
            "Guardian",
            row.guardian,
            "email_address",
        )

        if email:
            recipient_emails.append(email)
            guardian_names.append(
                row.guardian_name or row.guardian
            )

    # ---------------------------------------------------------
    # Remove duplicate email addresses
    # ---------------------------------------------------------

    recipient_emails = list(dict.fromkeys(recipient_emails))

    if not recipient_emails:
        frappe.log_error(
            f"No guardian email address found for Student {doc.student}",
            "Attendance Guardian Notification",
        )
        return None

    # ---------------------------------------------------------
    # Student information
    # ---------------------------------------------------------

    student_name = frappe.db.get_value(
        "Student",
        doc.student,
        "student_name",
    )

    attendance_date = frappe.utils.formatdate(doc.date)

    # ---------------------------------------------------------
    # Email content
    # ---------------------------------------------------------

    subject = (
        f"Attendance Notification - "
        f"{student_name} - {doc.status}"
    )

    message = f"""
    <p>Dear Parent/Guardian,</p>

    <p>
        This is an attendance notification from
        Queen Salote College.
    </p>

    <p>
        <strong>Student:</strong> {student_name}<br>
        <strong>Date:</strong> {attendance_date}<br>
        <strong>Attendance Status:</strong> {doc.status}
    </p>

    <p>
        Please contact the school if you require any
        further information regarding this attendance record.
    </p>

    <p>
        Kind regards,<br>
        Queen Salote College
    </p>
    """

    # ---------------------------------------------------------
    # Send email to parent/guardian email addresses
    # ---------------------------------------------------------

    frappe.sendmail(
        recipients=recipient_emails,
        subject=subject,
        message=message,
        reference_doctype="Student Attendance",
        reference_name=doc.name,
    )

    return {
        "student": doc.student,
        "student_name": student_name,
        "status": doc.status,
        "guardians": guardian_names,
        "recipients": recipient_emails,
    }



