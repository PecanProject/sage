Management Tradeoff Analysis: To estimate the relative benefits, tradeoffs, and identify potential synergies of implementing BMPs for organic processing tomatoes, plot and field data for production and environmental indicators were extrapolated to the entire area of the farm that was in tomato production during rainfed year one (Rainfed Y1) and Irrigated Y2. Tomato production using a winter fallow (tomatoes/fallow) was contrasted with BMP options that include winter cover crops, winter cover crops and tailwater ponds, or winter cover crops, tailwater ponds, and a tailwater return system. Mean results for marketable tomato yield, TSS, NO 3 -N, DRP, DOC leaching and/ or runoff loads, and N2O and CO2 soil emissions for the North Field, ditches and tailwater pond were multiplied by their respective areas, summed for the two seasons, and divided by their total summed areas. This produced a per hectare annual rate for each indicator. The N2O-N was converted to CO 2 equivalents using a conversion factor that takes into account emissions of N 2 O are 298 times greater than equal emissions of CO2 over 100-year time period (Forster et al. 2007).
⟦b:0076⟧

*Table 2 Soil properties taken at the o to 15 cm and 15 to 30 cm depth for each of the sampling areas for the 2005 and 2006 seasons. Means and standard errors of two years of sampling are shown (sample size = 3 to 6 per site).*
⟦b:0080⟧

| Site | Depth (cm) | Bulk density (g cm -3 ) | рН | EC (µS cm -1 ) | Total N (Mg ha -1 ) | Total C (Mg ha -1 ) | Olsen-P (mg kg -1 ) | Sand (g kg -1 ) | Silt (g kg -1 ) | Clay (g kg -1 ) |
|---|---|---|---|---|---|---|---|---|---|---|
| South field | 0 to 15 | 1.2 ± 0.0 | $7.2 \pm 0.0$ | 138.4 ± 18.5 | 2.5 ± 0.1 | 21.9 ± 1.3 | 33.7 ± 3.6 | 148.6 ± 39.3 | 707.0 ± 21.7 | 144.5 ± 20.5 |
|  | 15 to 30 | $1.3 \pm 0.0$ | $7.2 \pm 0.1$ | 144.7 ± 8.5 | $2.6 \pm 0.1$ | 20.5 ± 1.1 | 28.3 ± 4.5 | 140.6 ± 48.4 | 670.9 ± 29.5 | 188.4 ± 21.0 |
| North field | 0 to 15 | $1.3 \pm 0.0$ | $7.4 \pm 0.1$ | 120.2 ± 7.9 | $2.5 \pm 0.1$ | 22.4 ± 1.0 | 31.4 ± 1.2 | 119.0 ± 31.8 | 726.3 ± 23.5 | 154.7 ± 16.6 |
|  | 15 to 30 | $1.4 \pm 0.0$ | $7.2 \pm 0.1$ | 123.4 ± 8.3 | $2.6 \pm 0.1$ | $20.2 \pm 0.6$ | 28.9 ± 2.3 | 92.3 ± 29.6 | 741.6 ± 31.5 | 166.1 ± 17.8 |
| Tailwater pond | 0 to 15 | $1.2 \pm 0.1$ | $7.2 \pm 0.1$ | 148.4 ± 11.1 | $2.3 \pm 0.2$ | 19.7 ± 2.2 | 28.6 ± 5.1 | 80.9 ± 39.8 | 753.6 ± 23.4 | 165.5 ± 25.4 |
|  | 15 to 30 | $1.2 \pm 0.1$ | $7.3 \pm 0.1$ | 163.5 ± 22.3 | $2.2 \pm 0.2$ | 17.8 ± 2.3 | 26.5 ± 6.0 | 58.8 ± 28.0 | 753.0 ± 13.3 | 188.3 ± 30.5 |
| Ditches | 0 to 15 | $1.3 \pm 0.0$ | $7.3 \pm 0.1$ | 133.7 ± 10.5 | $2.5 \pm 0.2$ | 20.9 ± 2.0 | 44.9 ± 6.4 | 131.5 ± 40.5 | 700.1 ± 28.1 | 168.4 ± 12.9 |
|  | 15 to 30 | $1.5 \pm 0.1$ | $7.3 \pm 0.1$ | 130.0 ± 16.3 | $2.9 \pm 0.2$ | 19.3 ± 1.6 | 38.0 ± 6.5 | 200.5 ± 70.4 | 665.2 ± 58.0 | 134.3 ± 12.9 |
| Notes: EC = ele | ectrical con | ductivity. N | = nitrogen. | C = carbon. |  |  |  |  |  |  |
⟦b:0081⟧

Statistical Analysis. Concentrations and loads from water sampling were log transformed and checked for assumptions of normality with the Shapiro-Wilk test and equality of variance with the Levene test using the open source statistical package R version 2.11.1. (Helsel and Hirsch 1993). Means of each rainfall or irrigation event were considered replications and were compared through the entire season for each constituent. If assumptions of normality were met, a paired t-test was performed for either equal or unequal variances to test for treatment differences for concentration and load of each constituent for each season (Helsel and Hirsch 1993). For constituents that did not meet assumptions of normality and equality of variance, a Wilcoxon signed-rank test was used to test for treatment differences (Helsel and Hirsch 1993).
⟦b:0192⟧

To account for the differences in relative size of the fields, ditches, and tailwater ponds and distances between plots, a mixed model analysis of variance (ANOVA) was employed that incorporated a spatial covariance structure (Casanoves et al. 2005). The ANOVA tests that were significant were followed by Tukey's Honestly Significant Post Hoc Test (Zar 1974). Briefly, the mixed linear models were run after checking assumptions using the proc mixed statement in SAS version 9.3.1 (SAS Institute, Cary, North Carolina) combined with a power correlation function (POW model), which enables X,Y global positioning system coordinates to be used as a covariate. The POW model uses a onedimensional isotropic (same in all directions) power covariance, based in this case, on geographic information system coordinates and assumes no correlation between plots and homogeneous residual variances (Self and Liang 1987; Wolfinger 1993). The power
⟦b:0193⟧

correlation model is represented as $\rho_{u}^{dxij} \rho_{u}^{dyij}$, where $d^{xij}$ and $d^{yij}$ are the distances between plot i and plot j in the x and y directions and $\rho_{y}$ and $\rho_{y}$ are the unknown correlation parameters in the x and y directions (Casanoves et al. 2005). The degrees of freedom were adjusted as suggested by Kenward and Roger (1997). This methodology has been utilized and tested against other spatial and nonspatial models in agricultural systems and has been shown to be an effective means of dealing with spatial covariance (Bajwa and Mozaffari 2007; Bajwa and Vories 2007; Casanoves et al. 2005; Goncalves et al. 2007). The model, however, is unable to simultaneously account for repeated measurements; therefore, means were compared for each site without adjustment for variation over time.
⟦b:0194⟧

#### Results and Discussion
⟦b:0195⟧

Soil Properties. Soil properties of the four locations were similar across the farm sampling sites (table 2). All soils had a silt loam texture, total carbon (C) ranged from 19.7 to 22.4 Mg ha -1 (8.7 to 10.0 tn ac -1 ), and pH ranged from 7.2 to 7.4 at 0 to 15 cm (0 to 5.9 in) depth (table 2). The consistency of soil properties between sites indicates that a similar soil type occurred across the farm. Thus soil properties likely did not confound the analysis of the environmental outcomes of the BMPs.
⟦b:0196⟧

Runoff. Winter cover cropping improved the water quality of stormwater runoff during Rainfed Y1, but in rainfed year two (Rainfed Y2), no runoff was detected due to low rainfall. Compared to the fallow, water quality constituents in winter runoff (Rainfed Y1) were lower in cover cropped fields: 44% lower for EC and 80% lower for TSS (mg L -1 [ppm]) (table 3). Phosphorus as DRP (mg L -1 [ppm]), however, was 86%
⟦b:0197⟧

higher in discharge water from the covercropped field compared to the fallow. Higher concentrations of DRP may be a result of increased mobilization from the mustard cover crop. Other Brassica species have been shown to increase phosphorus availability through increased citric and malic acid in the rhizosphere (Eichler-Lobermann et al. 2008; Hoffland et al. 1992; Marschner et al. 2007).
⟦b:0198⟧

Sediment and nutrient loads were calculated for the stormwater runoff based on mean discharge for the five winter storm events (table 4). In Rainfed Y1, total discharge loads (kg ha -1 [lb ac -1 ]) were lower for cover cropped than fallow fields: 83% lower for TSS, 33% lower for NH 4 + –N, and 58% for DOC. Despite the large quantity of C in the cover crop biomass, there was no increase in DOC in runoff in either winter storm events or in the subsequent irrigation. Low DOC in runoff following the cover crop indicates gradual decomposition and possibly leaching losses.
⟦b:0199⟧

During Irrigated Y1, a total of 944 mm (37.2 in) of water (figure 2) was applied on the entire farm in 10 events, 35% of which discharged into the sediment trap (tomatoes discharge) (table 4). In Irrigated Y2, a mean total of 799 mm (31.5 in) of water was applied (figure 2) to the two North Field sections in 9 events, 25% of which discharged from the field section that had a prior mustard cover crop during the winter (tomatoes/ mustard) and 42% discharging from the field section that had been fallow (tomatoes/fallow). Mean irrigation discharge rates for the tomatoes/mustard and tomatoes/fallow rotations were not statistically different. Nor were there any differences in the concentrations or loads of measured constituents, except for pH, which was significantly higher in the discharge from the tomatoes/mustard field during the summer season.
⟦b:0200⟧

#### Table 3
⟦b:0204⟧

Mean concentration and standard errors of constituents analyzed from influent and discharge effluent from entire fields and paired sections of the North Field (F = fallow and M = mustard cover crop) during the two-year study by season. Means are given for the total number (n) of either irrigation or rainfall events. Discharge was not detected (ND) during the rainfed season in the second year due to unusually low precipitation. Measured constituents are pH, electrical conductivity (EC), total suspended solids (TSS), volatile suspended solids (VSS), nitrate ($NO_3^--N$), ammonium ($NH_4^+-N$), dissolved reactive phosphorus (DRP), and dissolved organic carbon (DOC).
⟦b:0205⟧

Season Treatment n рН EC (μS cm -1 ) TSS (g L -1 ) VSS (g L -1 ) NO 3 N (mg L -1 ) NH 4 +-N (mg L-1) DRP (mg L -1 ) DOC (mg L -1 ) Irrigated Y1 Irrigation influent 10 7.9 ± 0.1 795.9 ± 46.2 $0.04 \pm 0.01$ 0.015 ± 0.00 1.7 ± 0.3 0.1 ± 0.0 $0.1 \pm 0.0$ 2.3 ± 0.5 (South Field) Tomatoes discharge 10 $7.7 \pm 0.1$ 834.6 ± 33.8 7.27 ± 1.03 0.254 ± 0.05 $1.6 \pm 0.2$ $0.1 \pm 0.0$ $0.5 \pm 0.1$ $3.0 \pm 0.4$ Rainfed Y1 Fallow storm discharge 5 6.7 ± 0.0 115.1 ± 33.6** 0.07 ± 0.01* 0.002 ± 0.00 0.1 ± 0.0 0.1 ± 0.0 0.2 ± 0.0† 7.4 ± 0.0 (North Field) Mustard storm discharge 5 $6.7 \pm 0.1$ 64.5 ± 16.7** 0.01 ± 0.00* 0.006 ± 0.00 $0.1 \pm 0.0$ $0.1 \pm 0.0$ $0.4 \pm 0.1 \dagger$ $5.8 \pm 0.1$ Irrigated Y2 Irrigation influent 9 $7.9 \pm 0.1$ 600.0 ± 18.2 0.02 ± 0.01 0.017 ± 0.00 1.8 ± 0.3 $0.1 \pm 0.0$ $0.0 \pm 0.0$ 1.9 ± 0.4 (North Field) Tomatoes (F) discharge 9 8.1 ± 0.1* 644.0 ± 22.7 10.90 ± 3.85 0.259 ± 0.09 $2.2 \pm 0.2$ $0.2 \pm 0.1$ $0.3 \pm 0.0$ $3.9 \pm 0.9$ Tomatoes (M) discharge 9 8.3 ± 0.0* 611.4 ± 29.8 $3.99 \pm 0.93$ $0.271 \pm 0.15$ $1.6 \pm 0.3$ $0.1 \pm 0.0$ $0.3 \pm 0.0$ $3.3 \pm 0.4$ Rainfed Y2 Oats discharge 0 ND ND ND ND ND ND ND ND (South/North) Fallow discharge 0 ND ND ND ND ND ND ND ND
⟦b:0206⟧

Note: Significant difference were calculated using a paired t-test.
⟦b:0207⟧

#### Table 4
⟦b:0208⟧

Loads of constituents analyzed from the paired tomato fields (F = fallow and M = mustard cover crop), oat field, and tailwater pond during the two-year study by season. Loads are calculated from mean concentrations weighted by flow rates divided by the area from which the water discharged. Mean loads and standard errors are given as an event mean, where n is the total number of either irrigation or rainfall events. Discharge was not detected (ND) during the rainfed season in the second year (Rainfed Y2) due to unusually low precipitation. Measured constituents are total suspended solids (TSS), volatile suspended solids (VSS), nitrate ($NO_3^--N$), ammonium ($NH_4^+-N$), dissolved reactive phosphorus (DRP), and dissolved organic carbon (DOC).
⟦b:0209⟧

Treatment n Volume (mm event -1 ) TSS (kg ha -1 event -1 ) VSS (kg ha -1 event -1 ) NO 3 - -N (kg ha -1 event -1 ) NH 4 +-N (g ha -1 event -1 ) DRP (g ha -1 event -1 ) DOC (kg ha -1 event -1 ) Irrigated Y1 (South Field) orone , ovene , ovone , ovene , orone , ovone , - Overley Irrigation influent 10 94.4 ± 15.7 408.4 ± 161.3 13.9 ± 3.0 1.7 ± 0.4 92.8 ± 37.0 22.4 ± 6.2 1.9 ± 0.7 Tomatoes discharge 10 33.2 ± 6.3 2,384.6 ± 81.0 81.5 ± 17.4 0.6 ± 0.2 28.0 ± 14.4 70.0 ± 20.0 $0.9 \pm 0.2$ Rainfed Y1 (North Field) Fallow discharge 5 9.6 ± 3.3 5.0 ± 1.3* 0.2 ± 0.1 0.01 ± 0.01 8.3 ± 3.2* 22.3 ± 7.6 0.7 ± 0.2** Mustard discharge 5 5.4 ± 1.8 0.9 ± 0.4* $0.5 \pm 0.2$ $0.01 \pm 0.04$ 5.6 ± 2.7* 19.7 ± 5.3 0.3 ± .1** Tailwater pond discharge 5 ND ND ND ND ND ND ND Irrigated Y2 (North Field) Irrigation influent 9 88.8 ± 22.0 14.4 ± 7.9 19.0 ± 9.0 1.8 ± 0.5 35.2 ± 14.6 35.4 ± 14.9 1.5 ± 0.4 Tomatoes (F) discharge 9 37.9 ± 9.4 2,440.5 ± 849.2 93.6 ± 36.8 $0.9 \pm 0.3$ 22.6 ± 8.0 118.4 ± 42.0 $1.0 \pm 0.2$ Tomatoes (M) discharge 9 23.1 ± 3.1 902.3 ± 211.1 68.1 ± 39.1 $0.4 \pm 0.1$ 16.3 ± 5.3 60.8 ± 10.5 $0.7 \pm 0.1$ Tailwater pond influent 9 $32.3 \pm 9.7$ 1,046.1 ± 443.2** 51.8 ± 23.1** $0.6 \pm 0.3$ 21.1 ± 9.0 134.4 ± 49.6 $1.0 \pm 0.3$ Tailwater pond discharge 9 32.3 ± 6.7 30.7 ± 11.3** 4.4 ± 2.3** $0.7 \pm 0.2$ 50.9 ± 30.8 117.3 ± 32.2 1.4 ± 0.4 Rainfed Y2 (South/North Fie ld) Oats discharge 0 ND ND ND ND ND ND ND Fallow discharge 0 ND ND ND ND ND ND ND Tailwater pond discharge 0 ND ND ND ND ND ND ND
⟦b:0210⟧

Note: Significant difference were calculated using a paired t-test.
⟦b:0211⟧

[^footnote] † Wilcoxon signed ranked test p
⟦b:0215⟧

