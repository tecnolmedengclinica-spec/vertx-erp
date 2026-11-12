from django import forms
from .context import current_organization


class TenantModelForm(forms.ModelForm):
    required_css_class = 'required'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        organization = current_organization()
        if hasattr(self.instance, 'organization_id') and not self.instance.organization_id and organization:
            self.instance.organization_id = organization.pk
        # Querysets precisam ser recriados por request, nunca congelados no import.
        for name, field in self.fields.items():
            if isinstance(field, (forms.ModelChoiceField, forms.ModelMultipleChoiceField)):
                field.queryset = field.queryset.model._default_manager.all()
            if field.required:
                field.widget.attrs.setdefault('required', 'required')

    def clean(self):
        data = super().clean()
        organization = current_organization()
        if organization:
            for constraint in self._meta.model._meta.constraints:
                if 'organization' not in getattr(constraint, 'fields', ()):
                    continue
                values = {name: organization if name == 'organization' else data.get(name) for name in constraint.fields}
                # Vínculo técnico opcional com unicidade condicional.
                if any(value is None for value in values.values()):
                    continue
                existing = self._meta.model.objects.filter(**values)
                if self.instance.pk:
                    existing = existing.exclude(pk=self.instance.pk)
                if existing.exists():
                    name = next((name for name in constraint.fields if name != 'organization'), None)
                    self.add_error(name, 'Este valor já está cadastrado nesta organização.')
        return data


class OrganizationForm(forms.ModelForm):
    cnpj = forms.CharField(label='CNPJ', max_length=18, required=False)

    class Meta:
        from .models import Organization
        model = Organization
        fields = '__all__'

    def clean_cnpj(self):
        from .validators import normalize_cnpj, validate_cnpj
        value = normalize_cnpj(self.cleaned_data['cnpj'])
        validate_cnpj(value)
        return value
