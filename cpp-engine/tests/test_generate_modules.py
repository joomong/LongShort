"""Declaration discovery tests; actual compile/XML behavior is tested separately."""
import importlib.util
from pathlib import Path
import re
import sys
import tempfile
import unittest


script = Path(__file__).resolve().parents[1] / 'tools/generate_modules.py'
spec = importlib.util.spec_from_file_location('generate_modules', script)
generator = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = generator
spec.loader.exec_module(generator)


class ModuleDiscovery(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='longshort modules ')
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def header(self, source, name='Scale.h'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
        return path

    def parse(self, source, name='Scale.h'):
        return generator.parse_header(self.header(source, name))

    def test_parameter_types_and_cpp_initializers(self):
        module = self.parse('''#pragma once
struct MCustomScale : public Module {
    IntParameter periods{7}, minimum = IntParameter(2);
    FloatParameter factor{0.5}, offset{-2.0};
    void init() {}
    void calculate(unsigned, SignalVector&) {}
};''')
        self.assertEqual(module.tag, 'scale')
        self.assertEqual(module.class_name, 'MCustomScale')
        self.assertEqual([(p.name, p.kind) for p in module.parameters], [
            ('periods', 'IntParameter'), ('minimum', 'IntParameter'),
            ('factor', 'FloatParameter'), ('offset', 'FloatParameter')])
        text = generator.render([module])
        self.assertIn('attrs.find("periods")', text)
        self.assertNotIn('IntParameter(7)', text)  # C++ construction owns defaults.

    def test_class_access_labels_and_namespace(self):
        module = self.parse('''namespace desk::research {
class CustomScale final : public Module {
    double private_state = 0;
public:
    FloatParameter factor{1};
    void init() {}
    void calculate(unsigned, SignalVector&) {}
};
}''')
        self.assertEqual(module.class_name, 'desk::research::CustomScale')
        self.assertEqual([p.name for p in module.parameters], ['factor'])

    def test_declaration_attributes_are_preserved_by_cpp(self):
        for decoration in ['[[nodiscard]]', 'alignas(16)']:
            with self.subTest(decoration=decoration):
                module = self.parse('''struct DECORATION MDecorated : public Module {
    [[maybe_unused]] FloatParameter gain{0.5};
    alignas(8) IntParameter count{2};
    FloatParameter offset [[maybe_unused]] {0.0};
};'''.replace('DECORATION', decoration))
                self.assertEqual([p.name for p in module.parameters], ['gain', 'count', 'offset'])

    def test_methods_parameters_locals_and_helper_classes_are_not_fields(self):
        module = self.parse('''struct MCustomScale : Module {
    IntParameter getter() { IntParameter local{3}; return local; }
    const IntParameter& reference() const;
    static FloatParameter* pointer();
    void set(IntParameter argument);
    void calculate(unsigned, SignalVector&) {
        FloatParameter hidden{3};
        struct Local : public Module { IntParameter internal; };
        auto lambda = [] { IntParameter also_hidden; };
    }
    std::vector<IntParameter> values;
    FloatParameter factor{2};
};''')
        self.assertEqual([p.name for p in module.parameters], ['factor'])

    def test_comments_and_literals_do_not_create_modules_or_parameters(self):
        module = self.parse(r'''// struct Fake : Module { IntParameter bogus; };
/* FloatParameter noise; */
struct MCustomScale : Module {
    const char* text = "FloatParameter phantom; } ";
    const char* raw = R"body(struct Fake : Module { IntParameter phantom; };)body";
    IntParameter hex{0xF'F};
    IntParameter decimal{1'000};
};''')
        self.assertEqual([p.name for p in module.parameters], ['hex', 'decimal'])

    def test_optional_data_panel_binding(self):
        module = self.parse('''struct MCustomPanel : Module {
    const DataPanel* panel = nullptr;
    FloatParameter factor{1};
};''')
        self.assertTrue(module.bind_panel)
        self.assertIn('module->panel = panel;', generator.render([module]))

    def test_private_const_static_and_reference_parameters_are_rejected(self):
        for declaration in ['private: IntParameter count;', 'static IntParameter count;',
                            'const FloatParameter factor{1};', 'volatile IntParameter flag;',
                            'thread_local static IntParameter count;', 'IntParameter& reference;',
                            'IntParameter array[2];']:
            with self.subTest(declaration=declaration):
                with self.assertRaises(generator.DiscoveryError):
                    self.parse('struct MCustomScale : Module {'+declaration+'};')

    def test_input_parameter_conflicts_with_signal_routing(self):
        with self.assertRaisesRegex(generator.DiscoveryError, 'input is reserved'):
            self.parse('struct MCustomScale : Module { IntParameter input; };')

    def test_private_inheritance_multiple_types_and_templates_are_rejected(self):
        for source in ['class MCustomScale : Module {};',
                       'struct MCustomScale : private Module {};',
                       'struct MCustomScale : Module, Other {};',
                       'struct MOne : Module {}; struct MTwo : Module {};',
                       'template<class T> struct MCustomScale : Module {};']:
            with self.subTest(source=source):
                with self.assertRaises(generator.DiscoveryError):
                    self.parse(source)

    def test_anonymous_or_nested_only_module_is_not_silently_ignored(self):
        for source in ['namespace { struct MHidden : Module {}; }',
                       'struct Helper { struct MHidden : Module {}; };']:
            with self.subTest(source=source):
                with self.assertRaises(generator.DiscoveryError):
                    self.parse(source)

    def test_conditional_interface_rejected_and_header_guard_allowed(self):
        with self.assertRaisesRegex(generator.DiscoveryError, 'conditional'):
            self.parse('#if 0\nstruct MHidden : Module {};\n#endif\n')
        with self.assertRaisesRegex(generator.DiscoveryError, 'conditional'):
            self.parse('struct MCustomScale : Module {\n#ifdef FEATURE\nIntParameter count;\n#endif\n};')
        module = self.parse('#ifndef CUSTOM_SCALE_H\n#define CUSTOM_SCALE_H\nstruct MCustomScale : Module { IntParameter count; };\n#endif\n')
        self.assertEqual([p.name for p in module.parameters], ['count'])

    def test_helper_header_and_missing_directory(self):
        self.assertIsNone(self.parse('struct Helper { double value; };'))
        self.assertEqual(generator.discover(self.root / 'missing'), [])

    def test_reserved_tag_and_builtin_class_cannot_be_overridden(self):
        for builtin in ('Rank.h', 'Neutralize.h'):
            with self.assertRaisesRegex(generator.DiscoveryError, 'reserved'):
                self.parse('struct MCustomScale : Module {};', builtin)
            (self.root / builtin).unlink()
        self.header('struct MTsMean : Module {};')
        with self.assertRaisesRegex(generator.DiscoveryError, 'engine type'):
            generator.discover(self.root)

    def test_duplicate_tags_case_folded_and_duplicate_classes(self):
        self.header('struct MOne : Module {};', 'a/Scale.h')
        self.header('struct MTwo : Module {};', 'b/scale.hpp')
        with self.assertRaisesRegex(generator.DiscoveryError, 'duplicate module tag'):
            generator.discover(self.root)
        (self.root/'b/scale.hpp').unlink()
        self.header('struct MOne : Module {};', 'b/Other.h')
        with self.assertRaisesRegex(generator.DiscoveryError, 'duplicate module class'):
            generator.discover(self.root)

    def test_hidden_headers_ignored(self):
        self.header('struct MHidden : Module {};', '.old/Scale.h')
        self.header('struct MVisible : Module {};', 'Scale.h')
        self.assertEqual([m.class_name for m in generator.discover(self.root)], ['MVisible'])

    def test_unicode_and_spaces_remain_literal_in_include(self):
        module = self.parse('struct MCustomScale : Module {};', '한글 modules/Scale.h')
        text = generator.render([module])
        self.assertIn('한글 modules/Scale.h"', text)
        self.assertNotIn(r'\ud55c', text)

    def test_unrepresentable_include_path_rejected(self):
        module = self.parse('struct MCustomScale : Module {};', 'quote"dir/Scale.h')
        with self.assertRaisesRegex(generator.DiscoveryError, 'header path'):
            generator.render([module])

    def test_global_type_names_cannot_be_shadowed_by_factory_locals(self):
        module = self.parse('struct module : Module { FloatParameter factor{1}; };')
        text = generator.render([module])
        self.assertIn('make_unique<::module>()', text)
        self.assertIn('decltype(module->factor), ::FloatParameter', text)

    def test_output_stable_and_deleted_module_removed(self):
        path = self.header('struct MCustomScale : Module { FloatParameter factor{1}; };')
        output = self.root.parent / (self.root.name+'.cpp')
        try:
            text = generator.render(generator.discover(self.root))
            self.assertTrue(generator.write_if_changed(output, text))
            before = output.stat().st_mtime_ns
            self.assertFalse(generator.write_if_changed(output, text))
            self.assertEqual(before, output.stat().st_mtime_ns)
            path.unlink()
            empty = generator.render(generator.discover(self.root))
            self.assertTrue(generator.write_if_changed(output, empty))
            self.assertNotIn('MCustomScale', output.read_text())
        finally:
            output.unlink(missing_ok=True)


class ReservedTagsMatchEngineRegistry(unittest.TestCase):
    def test_reserved_tags_equal_builtin_modules_plus_structural_tags(self):
        source = (Path(__file__).resolve().parents[1] / 'src/compiler/dag_builder.cpp').read_text()
        modules = set(re.findall(r'register_module\("(\w+)"', source))
        self.assertTrue(modules)
        # Structural tags handled in compile_pipeline (data, signal, event, meta, ew), expansion (choice)
        # and the alpha wrapper (alpha); kept by hand because they are not registry entries.
        structural = {'data', 'signal', 'event', 'choice', 'alpha', 'meta', 'ew'}
        self.assertEqual(generator.RESERVED_TAGS, frozenset(modules | structural))


if __name__ == '__main__':
    unittest.main()
