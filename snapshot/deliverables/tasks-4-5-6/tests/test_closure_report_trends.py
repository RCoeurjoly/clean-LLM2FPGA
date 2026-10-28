import unittest
from scripts.pipeline import build_closure_measurement_report as report

class ReportTrendTests(unittest.TestCase):
    def fit(self, points):
        return report.fit_linear_trend(points, x_unit='parameters', y_unit='seconds')
    def test_exact_line(self):
        r=self.fit([(1,3),(2,5),(4,9)])
        self.assertEqual((r['slope'],r['intercept'],r['r_squared']),(2,1,1))
        self.assertEqual(r['residuals'],[0,0,0])
        self.assertEqual((r['x_unit'],r['y_unit']),('parameters','seconds'))
        self.assertFalse(r['predictive_acceptance'])
    def test_insufficient_or_degenerate_points(self):
        for p in [[(1,2),(2,3)],[(1,2),(1,3),(1,4)]]:
            with self.subTest(points=p),self.assertRaises(ValueError):self.fit(p)
    def test_invalid_values(self):
        for v in [None,True,float('nan'),float('inf')]:
            with self.subTest(value=v),self.assertRaises(ValueError):self.fit([(1,2),(2,3),(3,v)])
    def test_constant_response(self):
        r=self.fit([(1,7),(2,7),(3,7)])
        self.assertEqual(r['slope'],0)
        self.assertIsNone(r['r_squared'])
