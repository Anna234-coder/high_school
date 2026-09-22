import re

import frappe
from frappe import _
from frappe.utils import flt

from education.education.doctype.fee_schedule.fee_schedule import (
    create_sales_invoice,
)


def get_fee_structure_for_student(student, batch_name=None):
    """
    Determine the Fee Structure for the student.

    The Program Enrollment student_batch is preferred.
    Student.custom_form is only used as a fallback.

    Examples:
        N + F04 + C01 -> NF04C01
        N + TV + C01  -> NTVC01
    """

    student_doc = frappe.get_doc("Student", student)

    # Determine student stream/category.
    stream = (
        "I"
        if student_doc.custom_section == "INT"
        else "N"
    )

    # Prefer Program Enrollment.student_batch.
    batch_name = batch_name or student_doc.custom_form

    if not batch_name:
        frappe.throw(
            _(
                "Student {0} does not have a Student Batch/Form."
            ).format(student)
        )

    batch_name = str(batch_name)

    # TVET
    if "TVET" in batch_name.upper():
        form_code = "TV"

    # Normal Forms
    else:
        digits = re.findall(r"\d+", batch_name)

        if not digits:
            frappe.throw(
                _(
                    "Could not determine the student's form "
                    "from batch {0}."
                ).format(batch_name)
            )

        form_code = f"F0{digits[0]}"

    # Optional sibling ranking.
    use_sibling_rank = frappe.db.get_single_value(
        "Education Settings",
        "custom_use_sibling_ranking",
    )

    rank = ""

    if use_sibling_rank:
        rank = (
            frappe.db.get_value(
                "Student",
                student,
                "custom_sibling_rank",
            )
            or "C01"
        )

    return f"{stream}{form_code}{rank}"


def get_fee_schedules(fee_structure, enrollment_date=None):
    """
    Get submitted Fee Schedules for a Fee Structure.

    Academic Term is deliberately not used.

    Term fees are identified from their Fee Category names,
    for example:
        Term 1
        Term 2
        Term 3
        Term 4

    A term fee is included when the student's enrollment date
    is on or before that Fee Schedule's due date.

    This prevents students from being charged for term fees
    whose due dates had already passed before enrollment.
    """

    fee_schedules = frappe.get_all(
        "Fee Schedule",
        filters={
            "fee_structure": fee_structure,
            "docstatus": 1,
        },
        fields=[
            "name",
            "fee_structure",
            "posting_date",
            "due_date",
        ],
        order_by="posting_date asc",
    )

    if not fee_schedules:
        frappe.throw(
            _(
                "No submitted Fee Schedule was found "
                "for Fee Structure {0}."
            ).format(fee_structure)
        )

    selected_schedules = []

    for schedule_data in fee_schedules:

        schedule = frappe.get_doc(
            "Fee Schedule",
            schedule_data.name,
        )

        categories = [
            component.fees_category
            for component in schedule.components
            if component.fees_category
        ]

        # -----------------------------------------------------
        # Determine whether this schedule contains a Term fee.
        # -----------------------------------------------------

        term_categories = [
            category
            for category in categories
            if re.match(
                r"^Term\s+\d+$",
                str(category),
                re.IGNORECASE,
            )
        ]

        # -----------------------------------------------------
        # Term fees:
        # Do not charge a term whose due date has already
        # passed before the student enrolled.
        # -----------------------------------------------------

        if term_categories and enrollment_date:

            if (
                schedule_data.due_date
                and enrollment_date > schedule_data.due_date
            ):
                continue

        selected_schedules.append(schedule_data)

    if not selected_schedules:
        frappe.throw(
            _(
                "No applicable Fee Schedule was found for "
                "Fee Structure {0} and enrollment date {1}."
            ).format(
                fee_structure,
                enrollment_date,
            )
        )

    return selected_schedules




def apply_student_fee_discount(invoice_name, student):
    """
    Apply the student's custom fee discount.
    """

    discount_pct = frappe.db.get_value(
        "Student",
        student,
        "custom_fee_discount_percentage",
    )

    if not discount_pct:
        return None

    discount_pct = float(discount_pct)

    if discount_pct <= 0:
        return None

    invoice = frappe.get_doc(
        "Sales Invoice",
        invoice_name,
    )

    for item in invoice.items:
        item.discount_percentage = discount_pct

        item.discount_amount = flt(
            item.price_list_rate * discount_pct / 100.0,
            item.precision("discount_amount"),
        )

        item.rate = flt(
            item.price_list_rate - item.discount_amount,
            item.precision("rate"),
        )

    invoice.calculate_taxes_and_totals()

    invoice.flags.ignore_validate_update_after_submit = True

    invoice.save(
        ignore_permissions=True,
    )

    return discount_pct

def generate_custom_fees(enrollment, method=None):
    """
    Generate Sales Invoice(s) when a Program Enrollment
    is submitted.

    One invoice is created for each submitted Fee Schedule.

    Existing invoices for the same student and Fee Schedule
    are skipped to prevent duplicates.
    """

    if not enrollment.student:
        return None

    # ---------------------------------------------------------
    # 1. Determine Fee Structure
    # ---------------------------------------------------------

    fee_structure = get_fee_structure_for_student(
        student=enrollment.student,
        batch_name=enrollment.student_batch_name,
    )

    # ---------------------------------------------------------
    # 2. Get all submitted Fee Schedules
    # ---------------------------------------------------------

    fee_schedules = get_fee_schedules(
        fee_structure,
        enrollment_date=enrollment.enrollment_date,
    )

    created_invoices = []
    skipped_invoices = []

    # ---------------------------------------------------------
    # 3. Create one invoice per Fee Schedule
    # ---------------------------------------------------------

    for fee_schedule in fee_schedules:

        existing_invoice = frappe.db.get_value(
            "Sales Invoice",
            {
                "student": enrollment.student,
                "fee_schedule": fee_schedule.name,
                "docstatus": ["<", 2],
            },
            "name",
        )

        if existing_invoice:
            skipped_invoices.append(existing_invoice)
            continue

        invoice_name = create_sales_invoice(
            fee_schedule.name,
            enrollment.student,
        )

        apply_student_fee_discount(
            invoice_name,
            enrollment.student,
        )

        created_invoices.append(invoice_name)

    # ---------------------------------------------------------
    # 4. Show result
    # ---------------------------------------------------------

    frappe.msgprint(
        _(
            "Created {0} Sales Invoice(s) and skipped {1} "
            "existing invoice(s) for Fee Structure {2}."
        ).format(
            len(created_invoices),
            len(skipped_invoices),
            fee_structure,
        )
    )

    return {
        "created": created_invoices,
        "skipped": skipped_invoices,
    }
