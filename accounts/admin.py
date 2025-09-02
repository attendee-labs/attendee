# Register your models here.

from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from .models import Organization, User


class CreditsRangeListFilter(admin.SimpleListFilter):
    title = "credits range"
    parameter_name = "credits_range"

    def lookups(self, request, model_admin):
        return (
            ("negative", "Negative credits (< 0)"),
            ("low", "Low credits (0-5)"),
            ("medium", "Medium credits (5-100)"),
            ("high", "High credits (100+)"),
        )

    def queryset(self, request, queryset):
        if self.value() == "negative":
            return queryset.filter(centicredits__lt=0)
        elif self.value() == "low":
            return queryset.filter(centicredits__gte=0, centicredits__lt=500)
        elif self.value() == "medium":
            return queryset.filter(centicredits__gte=500, centicredits__lt=10000)
        elif self.value() == "high":
            return queryset.filter(centicredits__gte=10000)


class UserInline(admin.TabularInline):
    model = User
    extra = 0
    fields = ("email", "first_name", "last_name", "is_active", "is_staff")
    readonly_fields = ("email", "first_name", "last_name", "is_active", "is_staff")
    can_delete = False


class CreditTransactionForm(forms.Form):
    amount = forms.DecimalField(label="Credit Amount", max_digits=10, decimal_places=2, help_text="Positive values add credits, negative values subtract credits")
    description = forms.CharField(label="Description", max_length=255, required=True, widget=forms.Textarea(attrs={"rows": 3}))


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "display_credits", "is_webhooks_enabled", "created_at", "updated_at")
    list_filter = ("is_webhooks_enabled", "autopay_enabled", CreditsRangeListFilter)
    search_fields = ("name",)
    readonly_fields = ("name", "centicredits", "is_webhooks_enabled", "autopay_enabled", "autopay_threshold_credits_display", "autopay_amount_to_purchase_display", "autopay_stripe_customer_id", "autopay_charge_task_enqueued_at", "autopay_charge_failure_data", "created_at", "updated_at", "version", "add_credit_transaction_button")
    inlines = [UserInline]

    def display_credits(self, obj):
        return f"{obj.credits():.2f}"

    display_credits.short_description = "Credits"

    def add_credit_transaction_button(self, obj):
        url = reverse("admin:add-credit-transaction", args=[obj.pk])
        return format_html('<a class="button" href="{}">Add Credit Transaction</a>', url)

    add_credit_transaction_button.short_description = "Add Credits"

    def autopay_threshold_credits_display(self, obj):
        return f"{obj.autopay_threshold_credits():.2f}"

    autopay_threshold_credits_display.short_description = "Autopay Threshold (Credits)"

    def autopay_amount_to_purchase_display(self, obj):
        return f"${obj.autopay_amount_to_purchase_dollars():.2f}"

    autopay_amount_to_purchase_display.short_description = "Autopay Purchase Amount"

    fieldsets = (
        ("Organization Information", {"fields": ("name", "centicredits", "is_webhooks_enabled", "add_credit_transaction_button")}),
        ("Autopay Configuration", {"fields": ("autopay_enabled", "autopay_threshold_credits_display", "autopay_amount_to_purchase_display", "autopay_stripe_customer_id")}),
        ("Autopay Status", {"fields": ("autopay_charge_task_enqueued_at", "autopay_charge_failure_data")}),
        ("Metadata", {"fields": ("created_at", "updated_at", "version")}),
    )

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:org_id>/add-credit-transaction/",
                self.admin_site.admin_view(self.add_credit_transaction_view),
                name="add-credit-transaction",
            ),
        ]
        return custom_urls + urls

    def add_credit_transaction_view(self, request, org_id):
        org = Organization.objects.get(pk=org_id)

        if request.method == "POST":
            form = CreditTransactionForm(request.POST)
            if form.is_valid():
                amount = form.cleaned_data["amount"]
                description = form.cleaned_data["description"]

                # Convert decimal amount to centicredits (integer)
                centicredits = int(amount * 100)

                # Import here to avoid circular import
                from bots.models import CreditTransactionManager

                # Create the transaction
                CreditTransactionManager.create_transaction(organization=org, centicredits_delta=centicredits, description=description)

                self.message_user(request, f"Successfully added {amount} credits to {org.name}")
                return HttpResponseRedirect(reverse("admin:accounts_organization_change", args=[org_id]))
        else:
            form = CreditTransactionForm()

        context = {
            "title": f"Add Credit Transaction for {org.name}",
            "form": form,
            "opts": self.model._meta,
            "original": org,
        }
        return TemplateResponse(request, "admin/add_credit_transaction.html", context)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False  # Allow viewing but fields will be read-only

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("email", "first_name", "last_name", "organization", "is_staff")
    list_filter = ("is_staff", "is_superuser", "is_active", "organization")
    search_fields = ("email", "first_name", "last_name")
    ordering = ("email",)
    readonly_fields = ("email", "first_name", "last_name", "is_active", "is_staff", "is_superuser", "groups", "user_permissions", "organization", "last_login", "date_joined")

    fieldsets = (
        (None, {"fields": ("email",)}),
        ("Personal info", {"fields": ("first_name", "last_name")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Organization", {"fields": ("organization",)}),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )

    # Since all fields are read-only, we don't need add_fieldsets
    add_fieldsets = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False  # Allow viewing but fields will be read-only

    def has_delete_permission(self, request, obj=None):
        return False
